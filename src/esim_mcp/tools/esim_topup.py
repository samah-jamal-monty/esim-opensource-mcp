"""Phase 6B MCP tools: topping up an eSIM the user already owns.

Four free tools, and one that spends money **only in QA**.

What always ships
-----------------
* ``get_esim_topup_options`` -- a read. The platform's own list of top-up plans that are
  genuinely compatible with one of the caller's eSIMs, with the platform's own prices;
* ``prepare_esim_topup`` -- an MCP-local quote. It creates no order, moves no money, touches
  no SIM and writes nothing at the platform. Its whole job is to pin down *exactly* what the
  user is being asked to agree to, from the platform's own numbers;
* ``get_prepared_esim_topup`` / ``cancel_prepared_esim_topup`` -- read back and discard, both
  local.

``confirm_esim_topup``: QA only, and not idempotent
---------------------------------------------------
This tool performs a **real** top-up over the platform's *legacy*
``POST /user/bundle/assign-top-up`` -- the same route the portal uses. It is registered only
when :attr:`~esim_mcp.settings.Settings.esim_topup_execution_enabled` is on, that flag
defaults to off, and production settings **refuse to construct** with it set. Three
independent gates rest on it: the tool is not registered, the service refuses, and the
transport refuses the path.

The reason for all of that is idempotency, and it is not a matter of effort:

* ``POST /user/bundle/assign-top-up`` accepts no idempotency key and stores none;
* the ``Topup`` row it writes to ``user_order`` has no ICCID column and no request-key
  column, so two identical requests are byte-for-byte indistinguishable from one request
  sent twice;
* the wallet debit happens *before* provisioning, and the provisioning helper reports a
  failed eSIM-hub order by returning an exception object rather than raising it -- so an
  ambiguous outcome is not merely possible, it is the shape the existing flow already has.

Recognising a retry needs a durable key, which needs a schema change, which this branch does
not make. **So no code here tries to make a second request safe. It makes a second request
impossible instead**, and that is a different and weaker promise:

* one quote, one attempt, whatever the outcome. The attempt is counted *before* the request
  leaves, under a per-quote lock, so a request that dies in flight still locks the quote;
* an unknown outcome is terminal. Everywhere else in this codebase an unresolved write may be
  presented again with the key it already used; here there is no key, so asking again is a
  second top-up rather than a question about the first;
* the lock lives in this process. If the server restarts between sending a top-up and
  recording it, the lock is gone -- which is exactly why this capability does not go to
  production.

Consent
-------
``prepare_esim_topup`` returns everything the user has to hear -- the masked SIM, the plan,
the data, the validity, the exact amount and currency, the payment method, and that the
wallet is debited immediately -- and ``confirm_esim_topup`` requires the caller to echo that
amount back. A confirmation can therefore only come from something that actually read the
quote, and a mismatch refuses rather than charges.

Ownership
---------
The target eSIM is always resolved from the caller's own ``GET /user/my-esim`` list, so an
identifier that is not theirs is *not found* -- indistinguishable from one that does not
exist -- and it never reaches the platform. That resolution is repeated immediately before
execution, alongside compatibility, availability, price, currency, balance, expiry and
payment method: a quote is a snapshot, and the platform is the authority. The platform
enforces ownership independently on the compatibility read. The ICCID is only ever returned
masked.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field, SecretStr

from esim_mcp.client.account import AccountApiClient
from esim_mcp.client.topup import TopupApiClient
from esim_mcp.client.wallet import WalletApiClient
from esim_mcp.errors import (
    AuthenticationRequiredError,
    BackendTimeoutError,
    BackendUnavailableError,
    EsimMcpError,
    EsimNotFoundError,
    EsimTopupConfirmationRequiredError,
    EsimTopupExecutionUnavailableError,
    EsimTopupNotSupportedError,
    EsimTopupOptionsUnavailableError,
    EsimTopupOutcomeUnknownError,
    EsimTopupRejectedError,
    InsufficientWalletBalanceError,
    InvalidInputError,
    NoPurchasedEsimsError,
    NoTopupOptionsError,
    RateLimitedError,
    TopupBundleIncompatibleError,
    WalletUnavailableError,
)
from esim_mcp.models.account import BackendEsim, parse_esims
from esim_mcp.models.catalog import Bundle, parse_bundles
from esim_mcp.models.purchase import parse_legacy_topup_result
from esim_mcp.models.wallet import decimal_from_number
from esim_mcp.purchase.models import QuotedWallet, money_text
from esim_mcp.purchase.store import QuoteOwner
from esim_mcp.purchase.validation import evaluate_wallet, require_positive_price
from esim_mcp.selection.models import require_esim_number
from esim_mcp.selection.service import EsimSelectionService
from esim_mcp.session.identity import ClientIdentityProvider, derive_device_id
from esim_mcp.session.manager import SessionManager
from esim_mcp.settings import Settings
from esim_mcp.tools.guard import guarded
from esim_mcp.tools.purchase_preparation import user_ref_of
from esim_mcp.topup.models import (
    EsimTarget,
    EsimTopupExecution,
    EsimTopupExecutionStatus,
    EsimTopupQuote,
    QuotedTopupBundle,
    QuotedTopupPrice,
    TopupQuoteStatus,
    mask_iccid,
)
from esim_mcp.topup.service import (
    EsimTopupExecutionService,
    EsimTopupQuoteService,
    quote_ref,
    replay_execution_error,
    require_never_attempted,
    require_usable_topup_quote,
)

logger = logging.getLogger(__name__)

#: The sentence every result in this module carries. There is no branch on which it is false.
NOTHING_HAPPENED = "No order was created, nothing was charged and no data was added to the eSIM."

#: Said wherever a compatible-plans list is returned. The distinction it draws is the whole
#: reason this tool exists rather than a catalogue search.
COMPATIBILITY_NOTE = (
    "These are the only plans the platform will add to this particular eSIM. They come from the platform's own "
    "compatibility list for this SIM, not from the general catalogue: a catalogue plan that looks similar is not "
    "necessarily one the provider will attach to a SIM already in use, so never offer one as a top-up."
)

#: What to do with a list of options.
OPTIONS_NEXT_STEP = (
    "Show the user a short numbered list with the data, the validity and the price of each option, and ask which "
    "one they want. Do not read a plan code out -- refer to a plan by its name or by the number you gave it. "
    "Quote the prices exactly as they are here: they are the platform's own, in the platform's own currency."
)

#: The one payment method a top-up may be prepared for. See ``prepare_esim_topup``.
WALLET_PAYMENT_METHOD = "Wallet"

#: Repeated on a replayed result so the assistant does not describe one top-up as two.
REPLAY_NOTE = (
    "This top-up had already been carried out and this is the stored result of it, not a second top-up. The user "
    "was charged once. Never tell them they were charged twice."
)

#: The warning every confirmable quote must carry. It is the sentence the user has to hear
#: before they agree, and the one this phase most has to keep true.
WALLET_DEBIT_WARNING = (
    "Confirming this top-up debits the user's wallet IMMEDIATELY and adds the data to the SIM. It cannot be "
    "undone from here and there is no refund tool. It also CANNOT BE REPEATED SAFELY: the platform cannot "
    "recognise a second attempt as a repeat, so confirming twice would charge the user twice."
)

#: What an assistant must do before confirming. Written as an instruction because the
#: difference between "the user asked what it costs" and "the user agreed to pay" is the
#: whole of the consent story.
CONFIRMATION_NEXT_STEP = (
    "Read ALL of these back to the user in ordinary language: which eSIM (by its plan name or label, never the "
    "identifier), the top-up plan, the data, the validity, the exact amount and currency, and that it will be "
    "paid from their wallet balance immediately. Then ask them plainly whether to go ahead and WAIT for their "
    "answer. Only if they explicitly agree to that amount, call confirm_esim_topup once with this quote "
    "reference and the amount exactly as it appears in this result. Asking what a top-up costs is not agreement, "
    "and neither is silence."
)

#: What to say once a top-up is confirmed.
COMPLETED_NEXT_STEP = (
    "Tell the user plainly that the data was added to their eSIM and paid for from their wallet, and give them "
    "the plan name and the amount. Do not read the order reference or the eSIM identifier out unless they ask. "
    "If they want to see the new allowance, get_esim_consumption reads it from the platform -- the figures may "
    "take a moment to update, so never invent one."
)

#: The ceiling this phase has, stated where an assistant will read it.
EXECUTION_UNAVAILABLE_NOTE = (
    "This assistant cannot carry out the top-up itself. The platform cannot yet guarantee that a repeated "
    "request would not top the SIM up twice, so that step is deliberately not offered here rather than risked. "
    "Tell the user the quote plainly, then tell them the top-up has to be completed in the eSIM app or on the "
    "website, and give them the plan name and the amount so they can find it there. Never say the top-up was "
    "started, queued, reserved or paid for."
)

IccidArg = Annotated[
    str | None,
    Field(
        default=None,
        description=(
            "The identifier of the eSIM to top up, taken verbatim from a get_my_esims result in "
            "this conversation. Omit it when the user owns exactly one eSIM. Never invent an "
            "identifier, never take one from the user, and never pass one from another account."
        ),
    ),
]

EsimNumberArg = Annotated[
    int | None,
    Field(
        default=None,
        description=(
            "The number of the eSIM as shown in the most recent get_my_esims list for this "
            "signed-in user -- 1 for the first, 2 for the second, and so on. This is the "
            "normal way to pick one: when the user says 'top-ups for number 2', pass 2. It "
            "resolves only against that user's own current list, so call get_my_esims first if "
            "nothing has been listed yet, or if the user has signed out, signed in as someone "
            "else or reconnected since. Never invent a number and never carry one over from an "
            "earlier list."
        ),
    ),
]

TopupBundleCodeArg = Annotated[
    str,
    Field(
        description=(
            "The bundle_code of the top-up plan the user chose, taken verbatim from a "
            "get_esim_topup_options result for this same eSIM. Never use a code from the general "
            "catalogue, never invent one, and never derive one from a plan's name: only the "
            "platform can say what is compatible with a SIM already in use."
        )
    ),
]

QuoteIdArg = Annotated[
    str,
    Field(
        description=(
            "The reference returned by prepare_esim_topup in this conversation. Never invent, guess or edit one."
        )
    ),
]

ConfirmedAmountArg = Annotated[
    str,
    Field(
        description=(
            "The exact amount from the prepared top-up's confirm_amount field, copied verbatim "
            "-- for example '6.50'. This is the amount you must have read back to the user and "
            "which they must have explicitly agreed to pay. It is checked against the stored "
            "quote and a mismatch refuses the top-up; it never sets or changes a price. Never "
            "invent it, never round it, and never take it from the user."
        )
    ),
]


class EsimTopupService:
    """Backend-facing behaviour for the two eSIM top-up tools.

    Neither method here can reach a mutating route: the only backend calls it makes are
    ``GET /user/my-esim`` and ``GET /user/related-topup/...``, and the transport refuses
    every top-up execution route regardless.
    """

    def __init__(
        self,
        settings: Settings,
        account_client: AccountApiClient,
        topup_client: TopupApiClient,
        wallet_client: WalletApiClient,
        session_manager: SessionManager,
        identity_provider: ClientIdentityProvider,
        quotes: EsimTopupQuoteService,
        executions: EsimTopupExecutionService,
        selection_service: EsimSelectionService,
    ) -> None:
        self._settings = settings
        self._accounts = account_client
        self._topups = topup_client
        self._wallet = wallet_client
        self._sessions = session_manager
        self._identity_provider = identity_provider
        self._quotes = quotes
        self._executions = executions
        self._selection = selection_service

    # ------------------------------------------------------------------ internals

    async def _owner(self, ctx: Any | None) -> tuple[Any, str, Any, QuoteOwner]:
        """Resolve the caller and require a signed-in eSIM user.

        The owner key is built exactly as ``prepare_purchase`` builds it, from the verified
        MCP client identity plus a digest of the authenticated user id, so a top-up quote
        naming one user's SIM can only ever be read back by that same client-and-user pair.
        """
        identity = await self._identity_provider.resolve(ctx)
        device_id = derive_device_id(self._settings.salt_bytes(), identity)
        session = await self._sessions.require_session(identity.session_key)
        owner = QuoteOwner(session_key=identity.session_key, user_ref=user_ref_of(session))
        return identity, device_id, session, owner

    def _locale(self, value: str | None) -> str:
        return (value or "").strip() or self._settings.default_locale

    def _currency(self, value: str | None) -> str:
        return ((value or "").strip() or self._settings.default_currency).upper()

    async def _owned_esims(
        self, *, session_key: str, device_id: str, locale: str, currency: str
    ) -> list[BackendEsim]:
        async def operation(access_token: Any) -> Any:
            return await self._accounts.get_my_esims(
                device_id=device_id, access_token=access_token, locale=locale, currency=currency
            )

        data = await self._sessions.run_authenticated(
            session_key,
            operation,
            device_id=device_id,
            locale=locale,
            currency=currency,
            allow_refresh_replay=True,
        )
        return [esim for esim in parse_esims(data) if esim.iccid]

    @staticmethod
    def _resolve_target(esims: list[BackendEsim], iccid: str | None) -> BackendEsim:
        """Pick the eSIM to top up, from the caller's own list and from nothing else.

        The ownership boundary. An identifier not in this list is *not found* -- identical to
        one that exists nowhere -- so this tool cannot be used to discover that another
        account owns a particular SIM, let alone to top it up.
        """
        if not esims:
            raise NoPurchasedEsimsError()

        requested = (iccid or "").strip()
        if not requested:
            if len(esims) == 1:
                return esims[0]
            raise InvalidInputError(
                "This account has more than one eSIM, so it is not clear which one the user wants to top up. "
                "Show the user their eSIMs from get_my_esims as a short numbered list, ask which one, and call "
                "this again with that one's identifier."
            )

        for esim in esims:
            if esim.iccid == requested:
                return esim
        logger.info("topup_iccid_not_owned")
        raise EsimNotFoundError()

    @staticmethod
    def _require_toppable(esim: BackendEsim) -> BackendEsim:
        """Refuse a SIM the platform itself does not offer top-ups for.

        ``is_topup_allowed`` is the platform's own flag on the eSIM record. It is checked
        before the compatibility read rather than after, so a user is told plainly that this
        SIM cannot be topped up instead of being shown an empty list they might read as a
        temporary glitch.
        """
        if esim.is_topup_allowed is False:
            raise EsimTopupNotSupportedError()
        if not esim.bundle_code:
            # The compatibility route is keyed on the plan currently on the SIM. Without it
            # there is nothing to ask the platform about, and guessing one would be inventing
            # the very thing this tool exists to look up.
            raise EsimTopupNotSupportedError(
                "The platform does not record which plan is on this eSIM, so it cannot say what may be added to "
                "it. Tell the user top-ups are not available for this SIM and offer to look at a new plan."
            )
        return esim

    async def _compatible_bundles(
        self,
        *,
        session_key: str,
        device_id: str,
        esim: BackendEsim,
        locale: str,
        currency: str,
    ) -> list[Bundle]:
        """The platform's own compatibility list for one owned eSIM."""

        async def operation(access_token: Any) -> Any:
            return await self._topups.get_topup_options(
                device_id=device_id,
                access_token=access_token,
                bundle_code=esim.bundle_code or "",
                iccid=esim.iccid or "",
                locale=locale,
                currency=currency,
            )

        try:
            data = await self._sessions.run_authenticated(
                session_key,
                operation,
                device_id=device_id,
                locale=locale,
                currency=currency,
                allow_refresh_replay=True,
            )
        except (BackendTimeoutError, BackendUnavailableError):
            logger.warning("topup_options_unavailable")
            raise EsimTopupOptionsUnavailableError() from None
        except InvalidInputError:
            # The platform refuses an ICCID it cannot link to this user. Reaching it after
            # the ownership resolution above means the SIM has no provisioned profile.
            logger.info("topup_options_profile_unavailable")
            raise EsimTopupNotSupportedError() from None

        return [bundle for bundle in parse_bundles(data) if bundle.code and bundle.is_available]

    @property
    def execution_enabled(self) -> bool:
        """Whether this deployment may actually carry a top-up out.

        Off by default, and production settings refuse to construct with it on. Read as a
        property rather than captured at construction so a test can flip it without
        rebuilding the graph.
        """
        return bool(self._settings.esim_topup_execution_enabled) and not self._settings.is_production

    async def _wallet_snapshot(
        self,
        *,
        session_key: str,
        device_id: str,
        locale: str,
        currency: str,
        price: Decimal,
    ) -> QuotedWallet:
        """Read the authoritative balance and compare it with ``price`` in :class:`Decimal`.

        A snapshot, not a hold. It exists so a user hears "this leaves you with X" before
        agreeing, and so an obviously impossible top-up is never sent at all -- the platform
        re-checks the balance itself, and its answer is the one that governs.
        """

        async def operation(access_token: Any) -> Any:
            return await self._wallet.get_user_wallet(
                device_id=device_id, access_token=access_token, locale=locale, currency=currency
            )

        wallet = await self._sessions.run_authenticated(
            session_key,
            operation,
            device_id=device_id,
            locale=locale,
            currency=currency,
            allow_refresh_replay=True,
        )
        if wallet is None:
            raise WalletUnavailableError()
        return evaluate_wallet(balance=wallet.balance, currency=wallet.currency or currency, price=price)

    # ------------------------------------------------------- get_esim_topup_options

    async def get_esim_topup_options(
        self,
        *,
        esim_number: int | None = None,
        iccid: str | None = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """List the platform's compatible top-up plans for one owned eSIM. Reads only.

        ``esim_number`` picks from the caller's most recent numbered listing and skips the
        ``GET /user/my-esim`` read entirely -- the ICCID and the plan code the compatibility
        route needs are both already known. Ownership is unchanged by that: the listing is
        handed back only to the same client-and-user pair that recorded it, and the platform
        still resolves the ICCID against the caller's own profiles.
        """
        identity, device_id, _, owner = await self._owner(ctx)
        locale_value = self._locale(locale)
        currency_value = self._currency(currency)

        if esim_number is not None and (iccid or "").strip():
            raise InvalidInputError(
                "Pass either esim_number or iccid, not both -- they could name different eSIMs and this tool "
                "will not choose between them. Use the number from the most recent get_my_esims list."
            )

        if esim_number is not None:
            entry = await self._selection.resolve(owner, require_esim_number(esim_number))
            target = self._require_toppable(entry.to_backend_esim())
        else:
            esims = await self._owned_esims(
                session_key=identity.session_key, device_id=device_id, locale=locale_value, currency=currency_value
            )
            target = self._require_toppable(self._resolve_target(esims, iccid))
        bundles = await self._compatible_bundles(
            session_key=identity.session_key,
            device_id=device_id,
            esim=target,
            locale=locale_value,
            currency=currency_value,
        )

        logger.info("topup_options_read", extra={"option_count": len(bundles)})

        if not bundles:
            return {
                "status": "no_options",
                "esim": _esim_reference(target),
                "options": [],
                "total_count": 0,
                "order_created": False,
                "charged": False,
                "topped_up": False,
                "message": "The platform offers no top-up plans for this eSIM.",
                "next_step": (
                    "Tell the user plainly that there are no top-ups available for this eSIM. Never offer a plan "
                    "from the general catalogue instead -- only the platform can say what may be added to a SIM "
                    "already in use. Offer to look at a new plan for their destination if they want more data."
                ),
            }

        return {
            "status": "ok",
            "esim": _esim_reference(target),
            "total_count": len(bundles),
            "options": [_option(bundle) for bundle in bundles],
            "order_created": False,
            "charged": False,
            "topped_up": False,
            "message": NOTHING_HAPPENED,
            "compatibility_note": COMPATIBILITY_NOTE,
            "next_step": OPTIONS_NEXT_STEP,
        }

    # ------------------------------------------------------------ prepare_esim_topup

    async def prepare_esim_topup(
        self,
        *,
        bundle_code: str,
        iccid: str | None = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Quote one top-up. Creates no order, charges nothing and touches no SIM."""
        identity, device_id, session, owner = await self._owner(ctx)
        locale_value = self._locale(locale)
        currency_value = self._currency(currency)
        requested_code = _require_bundle_code(bundle_code)

        esims = await self._owned_esims(
            session_key=identity.session_key, device_id=device_id, locale=locale_value, currency=currency_value
        )
        target = self._require_toppable(self._resolve_target(esims, iccid))

        # Re-read rather than trusted: the compatibility list, the price and the
        # availability are all fetched fresh here, so a quote can never carry a figure the
        # platform did not state a moment ago -- and a plan that is no longer compatible
        # cannot be quoted at all.
        bundles = await self._compatible_bundles(
            session_key=identity.session_key,
            device_id=device_id,
            esim=target,
            locale=locale_value,
            currency=currency_value,
        )
        if not bundles:
            raise NoTopupOptionsError()

        chosen = next((bundle for bundle in bundles if bundle.code == requested_code), None)
        if chosen is None:
            logger.info("topup_bundle_not_compatible")
            raise TopupBundleIncompatibleError()

        amount = require_positive_price(
            decimal_from_number(chosen.price), chosen.currency_code or currency_value
        )
        price_currency = chosen.currency_code or currency_value
        wallet = await self._wallet_snapshot(
            session_key=identity.session_key,
            device_id=device_id,
            locale=locale_value,
            currency=price_currency,
            price=amount,
        )
        quote = await self._quotes.create(
            owner,
            identity_source=session.identity_source,
            target=_esim_target(target),
            bundle=_quoted_bundle(chosen),
            price=QuotedTopupPrice(amount=amount, currency=price_currency),
            # Wallet, and only wallet. The platform also settles a top-up by card or by DCB,
            # but a card top-up returns a payment intent this server cannot use and a DCB
            # top-up starts an OTP flow it cannot finish -- so neither is offered, and the
            # execution path refuses a quote that says anything else.
            payment_method=WALLET_PAYMENT_METHOD,
            wallet=wallet,
            locale=locale_value,
        )
        return _prepared_result(quote, execution_enabled=self.execution_enabled)

    # -------------------------------------------------------- get_prepared_esim_topup

    async def get_prepared_esim_topup(self, *, quote_id: str, ctx: Any | None = None) -> dict[str, Any]:
        """Read back one of this caller's own top-up quotes. Never touches the backend."""
        _, _, _, owner = await self._owner(ctx)
        quote = await self._quotes.get_record(owner, _require_quote_id(quote_id))
        status = quote.effective_status()
        if status is not TopupQuoteStatus.ACTIVE:
            return {
                "status": status.value,
                "quote_id": quote.quote_id,
                "esim": {"masked_iccid": quote.target.masked_iccid, "plan_name": quote.target.bundle_name},
                "topup": {"name": quote.bundle.name},
                "order_created": False,
                "charged": False,
                "topped_up": False,
                "message": NOTHING_HAPPENED,
                "note": (
                    "This prepared top-up has expired, so its price is no longer current. Offer to prepare it "
                    "again."
                    if status is TopupQuoteStatus.EXPIRED
                    else "This prepared top-up was cancelled -- explicitly, or because it was replaced by a "
                    "newer quote for the same plan, or because the user signed out. Offer to prepare it again."
                ),
            }
        return _prepared_result(quote, execution_enabled=self.execution_enabled)

    # ------------------------------------------------------------ confirm_esim_topup

    async def confirm_esim_topup(
        self,
        *,
        quote_id: str,
        confirmed_amount: str,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Carry out one prepared top-up from the wallet. **QA-only, and not idempotent.**

        This is the only method in this codebase that sends a request the platform cannot
        recognise as a repeat. Everything about its shape follows from that:

        * it refuses outright unless the QA flag is on;
        * it revalidates *everything* -- session, ownership, compatibility, availability,
          price, currency, balance, expiry, payment method -- immediately before sending,
          because a quote is a snapshot and the platform is the authority;
        * it requires the caller to echo the exact amount from the quote, so a confirmation
          can only come from something that actually read the quote back;
        * it counts the attempt **before** sending, under a per-quote lock, so a request that
          dies in flight still locks the quote;
        * it sends exactly once and never retries, at any level, for any reason;
        * it treats anything other than an explicit platform confirmation as unknown, and
          locks the quote rather than inviting a second attempt.
        """
        if not self.execution_enabled:
            logger.warning("esim_topup_execution_refused_disabled")
            raise EsimTopupExecutionUnavailableError()

        identity, device_id, _, owner = await self._owner(ctx)
        reference = _require_quote_id(quote_id)

        # Held across the backend call on purpose: two concurrent confirmations of the same
        # quote must not both reach the platform. For a route with no idempotency key that
        # race is the difference between one charge and two.
        async with self._executions.lock(reference):
            quote = await self._quotes.get_record(owner, reference)
            execution = await self._executions.acquire(owner, reference)

            replayed = self._replay_if_attempted(execution, quote)
            if replayed is not None:
                return replayed

            # Only reached when nothing was ever sent for this quote.
            require_usable_topup_quote(quote)
            _require_wallet_quote(quote)
            _require_confirmed_amount(quote, confirmed_amount)
            require_never_attempted(execution)

            target = await self._revalidate(
                identity=identity, device_id=device_id, session_key=identity.session_key, quote=quote
            )
            return await self._execute(
                quote=quote, execution=execution, owner=owner, device_id=device_id, target=target
            )

    def _replay_if_attempted(
        self, execution: EsimTopupExecution, quote: EsimTopupQuote
    ) -> dict[str, Any] | None:
        """Hand back the stored answer for a quote that has already been sent.

        Checked before every gate below, so a caller asking again about a *successful*
        top-up receives the receipt rather than a refusal -- and a caller asking again about
        anything else receives the refusal rather than a second charge.
        """
        if execution.status is EsimTopupExecutionStatus.SUCCEEDED and execution.result is not None:
            logger.info("esim_topup_replayed_locally", extra={"quote_ref": quote_ref(quote.quote_id)})
            return {**execution.result, "replayed": True, "replay_note": REPLAY_NOTE}
        if execution.status.is_terminal:
            raise replay_execution_error(execution)
        if execution.was_sent:
            # Sent, but the outcome was never recorded -- the process died mid-call. The
            # quote is locked exactly as if the outcome had been unknown, because it is.
            logger.error("esim_topup_attempt_without_outcome", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise EsimTopupOutcomeUnknownError(details=_unknown_details())
        return None

    async def _revalidate(
        self,
        *,
        identity: Any,
        device_id: str,
        session_key: str,
        quote: EsimTopupQuote,
    ) -> BackendEsim:
        """Re-establish every fact the quote asserts, from the platform, right now.

        A quote is a snapshot taken up to five minutes ago. Between then and now the SIM may
        have left the account, the plan may have stopped being compatible or been withdrawn,
        the price may have moved and the balance may have been spent. Each of those is
        re-read here and compared, and any disagreement refuses the top-up rather than
        charging an amount or a plan the user never agreed to.
        """
        stored_iccid = quote.target.iccid.get_secret_value()

        # Ownership, from the caller's own list. An ICCID that is not on this account is not
        # found -- and never reaches the platform.
        esims = await self._owned_esims(
            session_key=session_key, device_id=device_id, locale=quote.locale, currency=quote.price.currency
        )
        target = self._require_toppable(self._resolve_target(esims, stored_iccid))

        # Compatibility and availability, from the platform's own list for this SIM.
        bundles = await self._compatible_bundles(
            session_key=session_key,
            device_id=device_id,
            esim=target,
            locale=quote.locale,
            currency=quote.price.currency,
        )
        chosen = next((bundle for bundle in bundles if bundle.code == quote.bundle.bundle_code), None)
        if chosen is None:
            logger.info("esim_topup_no_longer_compatible")
            raise TopupBundleIncompatibleError(
                "The platform no longer offers this top-up for this eSIM, so it was not carried out and nothing "
                "was charged. Show the user the current options and ask them to choose again."
            )

        # Price and currency, as the platform states them now.
        current_amount = require_positive_price(
            decimal_from_number(chosen.price), chosen.currency_code or quote.price.currency
        )
        current_currency = (chosen.currency_code or quote.price.currency or "").strip().upper()
        if current_amount != quote.price.amount or current_currency != quote.price.currency.strip().upper():
            logger.info("esim_topup_price_changed")
            raise EsimTopupRejectedError(
                f"The price of this top-up changed to {money_text(current_amount)} {current_currency} since the "
                f"user was quoted {money_text(quote.price.amount)} {quote.price.currency}, so it was not carried "
                "out and nothing was charged. Tell the user the new amount and ask whether they still want it."
            )

        # Balance, as the platform states it now. The platform re-checks this itself and its
        # answer governs; this is the local half, so an obviously impossible top-up is never
        # sent at all.
        wallet = await self._wallet_snapshot(
            session_key=session_key,
            device_id=device_id,
            locale=quote.locale,
            currency=quote.price.currency,
            price=quote.price.amount,
        )
        if not wallet.sufficient:
            shortfall = money_text(wallet.shortfall) if wallet.shortfall is not None else "the difference"
            raise InsufficientWalletBalanceError(
                f"The wallet balance does not cover this top-up -- it is short by {shortfall} "
                f"{wallet.currency}. Nothing was charged. Tell the user the shortfall and offer to add money to "
                "their wallet first."
            )
        return target

    async def _execute(
        self,
        *,
        quote: EsimTopupQuote,
        execution: EsimTopupExecution,
        owner: QuoteOwner,
        device_id: str,
        target: BackendEsim,
    ) -> dict[str, Any]:
        """Send exactly one top-up and interpret the answer. No retry, ever."""
        # Counted before the call. A request that dies in flight has still been sent, and the
        # quote has to be locked as if it succeeded -- that is the only ordering that cannot
        # charge somebody twice.
        execution = await self._executions.record_attempt(execution)

        # Refreshed *before* the call rather than replayed after a 401: a replay would be a
        # second POST, and this is the one call in this codebase that must never be repeated.
        session = await self._sessions.ensure_fresh_session(
            owner.session_key,
            device_id=device_id,
            locale=quote.locale,
            currency=quote.price.currency,
        )

        logger.warning(
            "esim_topup_execution_sending",
            extra={"quote_ref": quote_ref(quote.quote_id), "attempt": execution.attempts, "idempotent": False},
        )

        try:
            outcome = await self._topups.execute_topup(
                device_id=device_id,
                access_token=session.access_token,
                iccid=quote.target.iccid.get_secret_value(),
                bundle_code=quote.bundle.bundle_code,
                locale=quote.locale,
                currency=quote.price.currency,
                read_timeout=self._settings.purchase_read_timeout,
            )
        except (BackendTimeoutError, BackendUnavailableError):
            # The request may have been executed in full before the connection died. This is
            # the branch where saying "it failed" would be a lie with a price attached -- and
            # where "try again" would be a second charge.
            error = EsimTopupOutcomeUnknownError(details=_unknown_details())
            await self._executions.mark_unresolved(execution, error)
            logger.error("esim_topup_outcome_unknown", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise error from None

        if outcome.is_success:
            return await self._on_success(
                data=outcome.data, quote=quote, execution=execution, owner=owner, target=target
            )

        raise await self._on_failure(
            outcome_status=outcome.effective_status,
            hint=outcome.error_hint,
            parsed=outcome.parsed,
            quote=quote,
            execution=execution,
        )

    async def _on_success(
        self,
        *,
        data: Any,
        quote: EsimTopupQuote,
        execution: EsimTopupExecution,
        owner: QuoteOwner,
        target: BackendEsim,
    ) -> dict[str, Any]:
        """Interpret a successful envelope. Only an explicit confirmation counts.

        The platform's wallet branch answers with a ``PaymentIntentResponse`` carrying an
        ``order_id`` and ``payment_status: COMPLETED``. Anything else inside a 2xx -- a
        missing order id, a pending status, a body this server cannot read -- is **not** a
        success, and is recorded as unknown rather than as a failure, because the wallet was
        very likely debited before whatever went wrong.
        """
        result = parse_legacy_topup_result(data)
        if result is None or not result.order_id or not result.is_completed:
            error = EsimTopupOutcomeUnknownError(details=_unknown_details())
            await self._executions.mark_unresolved(execution, error)
            logger.error(
                "esim_topup_success_envelope_unusable",
                extra={"quote_ref": quote_ref(quote.quote_id), "has_order_id": bool(result and result.order_id)},
            )
            raise error

        payload = _completed_result(quote, result, target)
        await self._executions.mark_succeeded(execution, payload)
        # Consumed only here: after a confirmed, completed top-up, so this quote can never
        # become a second one.
        await self._quotes.consume(owner, quote.quote_id)
        logger.warning(
            "esim_topup_completed",
            extra={
                "quote_ref": quote_ref(quote.quote_id),
                "order_created": True,
                "charged": True,
                "topped_up": True,
            },
        )
        return payload

    async def _on_failure(
        self,
        *,
        outcome_status: int,
        hint: str,
        parsed: bool,
        quote: EsimTopupQuote,
        execution: EsimTopupExecution,
    ) -> EsimMcpError:
        """Classify a non-successful answer, record it, and return the error to raise.

        Two dispositions, and the line between them is whether the platform *refused* or
        merely *did not answer usefully*. A refusal (4xx with a body) means nothing was
        charged and may be said so. Anything else -- a 5xx, an unreadable body, a 2xx that
        was not a completion -- is unknown, because the wallet debit happens early in the
        platform's flow and a late failure leaves it done.
        """
        error, charged_known_false = _classify_topup_failure(outcome_status, hint, parsed)

        if charged_known_false:
            await self._executions.mark_failed(execution, error)
        else:
            await self._executions.mark_unresolved(execution, error)

        logger.warning(
            "esim_topup_not_completed",
            extra={
                "quote_ref": quote_ref(quote.quote_id),
                "http_status": outcome_status,
                "error_code": error.code,
                "charged": False if charged_known_false else None,
            },
        )
        return error

    async def cancel_prepared_esim_topup(self, *, quote_id: str, ctx: Any | None = None) -> dict[str, Any]:
        """Discard one of this caller's own top-up quotes. Local only."""
        _, _, _, owner = await self._owner(ctx)
        quote = await self._quotes.cancel(owner, _require_quote_id(quote_id))
        return {
            "status": "cancelled",
            "quote_id": quote.quote_id,
            "order_cancelled": False,
            "charged": False,
            "topped_up": False,
            "message": "The local top-up quote was discarded. No order existed at the platform.",
            "note": (
                "Nothing was refunded or reversed because nothing was ever ordered or charged. Confirm the "
                "cancellation plainly and never tell the user an order was cancelled."
            ),
        }


# ------------------------------------------------------------------------ result shaping


def _prune(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None and value != ""}


def _esim_reference(esim: BackendEsim) -> dict[str, Any]:
    """How a result names the SIM: masked, and by the plan the user recognizes."""
    return _prune(
        {
            "masked_iccid": mask_iccid(esim.iccid),
            "label": esim.label_name,
            "plan_name": esim.bundle_marketing_name or esim.bundle_name or esim.display_title,
            "plan_started": esim.plan_started,
            "bundle_expired": esim.bundle_expired,
            "topup_allowed": esim.is_topup_allowed,
        }
    )


def _option(bundle: Bundle) -> dict[str, Any]:
    """One compatible top-up as a numbered option in a chat reply."""
    option: dict[str, Any] = {
        "bundle_code": bundle.code,
        "name": bundle.name,
        "data": bundle.data_display,
        "unlimited": bundle.is_unlimited,
        "validity": bundle.validity_text,
        "price": bundle.price_text,
    }
    if bundle.price is not None:
        option["price_amount"] = money_text(Decimal(str(bundle.price)))
    if bundle.currency_code:
        option["currency"] = bundle.currency_code
    if bundle.plan_type:
        option["plan_type"] = bundle.plan_type
    return option


def _esim_target(esim: BackendEsim) -> EsimTarget:
    """Copy the platform's own record of an owned eSIM into the quote's typed snapshot."""
    return EsimTarget(
        iccid=SecretStr(esim.iccid or ""),
        masked_iccid=mask_iccid(esim.iccid) or "",
        bundle_code=esim.bundle_code or "",
        bundle_name=esim.bundle_marketing_name or esim.bundle_name or esim.display_title,
        plan_started=esim.plan_started,
        bundle_expired=esim.bundle_expired,
        topup_allowed=esim.is_topup_allowed,
        label=esim.label_name,
        order_reference=esim.order_number,
    )


def _quoted_bundle(bundle: Bundle) -> QuotedTopupBundle:
    """Copy the authoritative top-up plan facts into the quote's own typed snapshot."""
    return QuotedTopupBundle(
        bundle_code=bundle.code or "",
        name=bundle.name,
        data_display=bundle.data_display,
        unlimited=bundle.is_unlimited,
        validity_display=bundle.validity_text,
        validity_days=bundle.validity_days,
        plan_type=bundle.plan_type,
        activation_policy=bundle.activity_policy,
        countries_count=bundle.count_countries or len(bundle.countries),
    )


def _prepared_result(quote: EsimTopupQuote, *, execution_enabled: bool) -> dict[str, Any]:
    """The structured facts a prepared top-up reports.

    ``order_created``, ``charged`` and ``topped_up`` are always present and always ``False``:
    preparing has no branch that could make any of them true, which is exactly why they are
    safe to state unconditionally.

    ``execution_enabled`` decides which of two results this is. With execution off, it is a
    quote that ends in "finish this elsewhere". With execution on (QA), it is the *final
    confirmation prompt*: it carries the masked SIM, the plan, the data, the validity, the
    exact amount and currency, the payment method, the balance, the immediate-debit warning
    and the amount the confirmation has to echo back.
    """
    result: dict[str, Any] = {
        "status": "prepared",
        "quote_id": quote.quote_id,
        "expires_at": quote.expires_at.isoformat(),
        "expires_in_seconds": quote.seconds_remaining(),
        "expiry_is_local_bookkeeping": True,
        "expiry_note": (
            "This expiry is internal to this assistant and is not a price guarantee. Never tell the user the "
            "price is held, locked or reserved, and never quote this countdown to them."
        ),
        "esim": _prune(
            {
                "masked_iccid": quote.target.masked_iccid,
                "label": quote.target.label,
                "plan_name": quote.target.bundle_name,
                "plan_started": quote.target.plan_started,
                "bundle_expired": quote.target.bundle_expired,
            }
        ),
        "topup": _prune(
            {
                "bundle_code": quote.bundle.bundle_code,
                "name": quote.bundle.name,
                "data": quote.bundle.data_display,
                "unlimited": quote.bundle.unlimited,
                "validity": quote.bundle.validity_display,
                "plan_type": quote.bundle.plan_type,
                "activation_policy": quote.bundle.activation_policy,
            }
        ),
        "pricing": {
            "amount": money_text(quote.price.amount),
            "currency": quote.price.currency,
        },
        "payment_method": quote.payment_method,
        "order_created": False,
        "charged": False,
        "topped_up": False,
        "can_be_completed_here": execution_enabled,
        "message": NOTHING_HAPPENED,
        "price_note": (
            "This is the platform's own price for this top-up, read a moment ago. Give the user this figure as "
            "it stands and do not attach caveats the platform never made."
        ),
    }
    if quote.wallet is not None:
        wallet: dict[str, Any] = {
            "balance": money_text(quote.wallet.balance),
            "currency": quote.wallet.currency,
            "sufficient": quote.wallet.sufficient,
            "balance_is_a_snapshot": True,
        }
        if quote.wallet.estimated_remaining_balance is not None:
            wallet["estimated_remaining_balance"] = money_text(quote.wallet.estimated_remaining_balance)
        if quote.wallet.shortfall is not None:
            wallet["shortfall"] = money_text(quote.wallet.shortfall)
        result["wallet"] = wallet
        if not quote.wallet.sufficient:
            result["can_be_completed_here"] = False
            result["precondition"] = (
                "The wallet balance does not cover this top-up, so it cannot go ahead. Tell the user the "
                "shortfall and offer to add money to their wallet first."
            )

    if not result["can_be_completed_here"]:
        result["completion_note"] = EXECUTION_UNAVAILABLE_NOTE
        result["next_step"] = (
            "Read the eSIM, the top-up plan and the amount back to the user, and say plainly that nothing was "
            "ordered, nothing was charged and no data was added. Then tell them this assistant cannot complete "
            "the top-up and that they finish it in the eSIM app or on the website. Never say it is reserved, "
            "queued, started or paid for, and never offer to complete it."
        )
        return result

    # Confirmable. Everything the user has to hear before agreeing is in this result, and
    # ``confirm_amount`` is the value the confirmation has to echo back -- so a confirmation
    # can only ever come from something that actually read this.
    result["requires_explicit_confirmation"] = True
    result["confirm_amount"] = money_text(quote.price.amount)
    result["debit_warning"] = WALLET_DEBIT_WARNING
    result["idempotent"] = False
    result["repeat_note"] = (
        "Call confirm_esim_topup AT MOST ONCE for this quote. The platform cannot recognise a second attempt as "
        "a repeat, so there is no safe retry: if the outcome is unclear, check the user's eSIMs, data usage, "
        "wallet balance or order history instead of confirming again."
    )
    result["next_step"] = CONFIRMATION_NEXT_STEP
    return result


def _completed_result(
    quote: EsimTopupQuote,
    result: Any,
    target: BackendEsim,
) -> dict[str, Any]:
    """The structured facts a confirmed top-up reports.

    ``charged`` and ``topped_up`` are ``True`` here and nowhere else in this module. They are
    only ever set on the one branch where the platform confirmed a completed top-up with an
    order reference, which is what makes them trustworthy to read out.
    """
    payload: dict[str, Any] = {
        "status": "topped_up",
        "quote_id": quote.quote_id,
        "order_created": True,
        "charged": True,
        "topped_up": True,
        "payment_method": quote.payment_method,
        "esim": _prune(
            {
                "masked_iccid": quote.target.masked_iccid,
                "label": quote.target.label,
                "plan_name": target.bundle_marketing_name or target.bundle_name or quote.target.bundle_name,
            }
        ),
        "topup": _prune(
            {
                "bundle_code": quote.bundle.bundle_code,
                "name": quote.bundle.name,
                "data": quote.bundle.data_display,
                "validity": quote.bundle.validity_display,
                "activation_policy": quote.bundle.activation_policy,
            }
        ),
        "pricing": {
            "amount": money_text(quote.price.amount),
            "currency": quote.price.currency,
        },
        "message": "The top-up was added to the eSIM and paid for from the wallet.",
        "price_note": (
            "This is the amount the quote showed the user. The platform settles the final amount itself, so "
            "present it as what the top-up cost rather than as a receipt total."
        ),
        "repeat_note": (
            "This top-up is done. Do NOT confirm it again and do NOT prepare another one unless the user asks "
            "for more data on top."
        ),
        "next_step": COMPLETED_NEXT_STEP,
    }
    if getattr(result, "order_id", None):
        payload["order_id"] = result.order_id
    if getattr(result, "payment_status", None):
        payload["payment_status"] = result.payment_status
    return payload


def _unknown_details() -> dict[str, Any]:
    """Safe payload for an outcome this server never learned.

    Every field here says the same thing in a different way, because this is the branch a
    model is most likely to want to "fix" by trying again -- and trying again is the one
    action that turns an uncertain single charge into a certain double one.
    """
    return {
        "charged": None,
        "topped_up": None,
        "retry_safe": False,
        "new_topup_safe": False,
        "next_step": "check_account_state",
        "check_with": ["get_my_esims", "get_esim_consumption", "get_user_profile", "get_order_history"],
        "retry_note": (
            "Do NOT confirm this top-up again and do NOT prepare a replacement. The platform cannot tell a "
            "repeat apart from a second genuine top-up, so a retry could charge the user twice. Find out what "
            "actually happened instead: check their eSIMs, their data usage, their wallet balance or their "
            "order history."
        ),
    }


#: Backend error labels matched for internal classification only. The platform's envelope has
#: no machine-readable code: ``title`` is a localized string that falls back to the internal
#: error key when no translation exists, so both forms are matched here. Neither is ever
#: forwarded to an MCP client.
_BUSINESS_ERROR_MATCHERS: tuple[tuple[tuple[str, ...], type[EsimMcpError]], ...] = (
    (("insufficient_wallet_balance", "insufficient wallet"), InsufficientWalletBalanceError),
    (("wallet_not_found", "wallet not found"), WalletUnavailableError),
    (("bundle_not_available", "bundle not available"), TopupBundleIncompatibleError),
    (("invalid_payment_type", "payment type"), EsimTopupRejectedError),
)


def _classify_topup_failure(status: int, hint: str, parsed: bool) -> tuple[EsimMcpError, bool]:
    """Map one unsuccessful answer to a typed error and whether "nothing was charged" holds.

    The boolean is the load-bearing half. It is ``True`` only where the platform *refused*
    the request with a readable answer -- a 4xx it explained -- because the platform's flow
    validates the bundle and the balance before it debits anything. Everything else is
    ``False``, meaning **unknown**: the wallet debit happens early, so a 5xx, an unreadable
    body or a dropped answer can all arrive after the money has already moved.
    """
    lowered = (hint or "").strip().lower()

    if status in (401, 403):
        return AuthenticationRequiredError(
            "The eSIM session was refused by the platform, so the top-up was not carried out and nothing was "
            "charged. Ask the user to sign in again, then prepare the top-up again and confirm the new amount."
        ), True

    if status == 404:
        return EsimTopupRejectedError(
            "This eSIM platform did not accept the top-up request, so nothing was charged. Tell the user the "
            "top-up could not be carried out here and that they can finish it in the eSIM app or on the website."
        ), True

    if status == 429:
        return RateLimitedError(
            "The eSIM platform is rate limiting this client, so the top-up was not carried out and nothing was "
            "charged. Tell the user and offer to prepare it again shortly."
        ), True

    if status in (400, 409, 422) and parsed:
        for needles, error_type in _BUSINESS_ERROR_MATCHERS:
            if any(needle in lowered for needle in needles):
                return error_type(), True
        return EsimTopupRejectedError(), True

    # Everything below is unknown, not failed. The wallet may already have been debited.
    return EsimTopupOutcomeUnknownError(details=_unknown_details()), False


def _require_wallet_quote(quote: EsimTopupQuote) -> EsimTopupQuote:
    """Gate a stored quote before anything is sent for it. Wallet-only by contract."""
    if quote.payment_method != WALLET_PAYMENT_METHOD:
        raise EsimTopupRejectedError(
            "This prepared top-up is not a wallet top-up, and wallet is the only method this assistant can "
            "settle. Prepare the top-up again. Nothing was charged."
        )
    return quote


def _require_confirmed_amount(quote: EsimTopupQuote, confirmed_amount: str) -> None:
    """Refuse unless the caller echoed back the exact amount from the quote.

    Verification only: this value never prices anything and never reaches the platform. It
    can cause a refusal and nothing else, which is the point -- it makes "the model actually
    read the amount back" a checkable property rather than a hope, and it catches a
    confirmation aimed at the wrong quote.
    """
    expected = money_text(quote.price.amount)
    candidate = (confirmed_amount or "").strip().replace(",", "")
    if not candidate:
        raise EsimTopupConfirmationRequiredError(
            "No amount was confirmed. Read the eSIM, the plan and the exact amount back to the user, get their "
            "explicit agreement, and confirm again with the amount exactly as the prepared quote states it. "
            "Nothing was ordered and nothing was charged."
        )
    try:
        matches = Decimal(candidate) == quote.price.amount
    except (InvalidOperation, ValueError):
        matches = False
    if not matches:
        logger.info("esim_topup_confirmation_amount_mismatch")
        raise EsimTopupConfirmationRequiredError(
            f"The confirmed amount does not match this prepared top-up, which is {expected} "
            f"{quote.price.currency}. Nothing was ordered and nothing was charged. Read that amount back to the "
            "user, get their explicit agreement to it, and confirm again with exactly that figure."
        )


def _require_bundle_code(bundle_code: str) -> str:
    candidate = (bundle_code or "").strip()
    if not candidate:
        raise InvalidInputError(
            "A top-up plan code from a get_esim_topup_options result in this conversation is required. Never "
            "invent one, and never use a code from the general catalogue."
        )
    return candidate


def _require_quote_id(quote_id: str) -> str:
    candidate = (quote_id or "").strip()
    if not candidate:
        raise InvalidInputError(
            "A reference from a prepare_esim_topup result in this conversation is required. Never invent one."
        )
    return candidate


def register_esim_topup_tools(server: MCPServer, service: EsimTopupService) -> None:
    """Bind the Phase 6B tools onto an :class:`MCPServer` instance.

    Four free tools always: a read, a quote, a read-back and a local discard.

    ``confirm_esim_topup`` is registered **only** when this deployment has the QA execution
    flag on, so in every other deployment a model cannot call it -- not because it refuses,
    but because it does not exist. There is deliberately no ``buy_topup`` and no
    ``assign_topup`` under any configuration.
    """

    @server.tool(
        name="get_esim_topup_options",
        title="List the top-up plans available for an eSIM the user owns",
        description=(
            "Read the eSIM platform's own list of top-up plans that can be added to one of the "
            "signed-in user's existing eSIMs, with the platform's own data allowance, validity "
            "and price for each. Reads only -- it buys nothing and adds nothing to the SIM.\n"
            "WHEN: the user wants more data on an eSIM they already have -- \"can I add more "
            "data\", \"top up my eSIM\", \"I'm running out, what are my options\".\n"
            "FIRST: the user must be signed in. Check get_login_status and run the normal login "
            "conversation if they are not.\n"
            "WHICH eSIM: omit both arguments when the user owns exactly one. When they own "
            "several, show them the numbered list from get_my_esims, ask which one, and pass "
            "that number as esim_number: 'top-ups for number 2' means esim_number=2. The number "
            "resolves against that user's own most recent list, so this tool does NOT re-read "
            "their eSIMs and the number must come from a list shown in this session. Pass iccid "
            "only if you already hold the identifier, and never pass both.\n"
            "IF IT ANSWERS 'esim_selection_unavailable': the numbered list that number came "
            "from is gone -- call get_my_esims for a new list and use a number from that one. "
            "If it answers 'esim_selection_out_of_range', that number is not on the list: show "
            "the list again and ask which eSIM they mean.\n"
            "THIS IS THE ONLY SOURCE OF TOP-UP PLANS. Never offer a plan from "
            "find_bundles_by_country, find_bundles_by_region or any other catalogue tool as a "
            "top-up: only the platform can say what may be added to a SIM already in use.\n"
            "AFTER SUCCESS: show a short numbered list with the data, the validity and the "
            "price, and ask which one they want. Do not read plan codes out. If the list is "
            "empty, say plainly that there are no top-ups for this eSIM and offer a new plan "
            "instead -- never present a catalogue plan as a top-up."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def get_esim_topup_options(
        esim_number: EsimNumberArg = None,
        iccid: IccidArg = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "get_esim_topup_options",
            lambda: service.get_esim_topup_options(
                esim_number=esim_number, iccid=iccid, locale=locale, currency=currency, ctx=ctx
            ),
        )

    @server.tool(
        name="prepare_esim_topup",
        title="Quote a top-up for an eSIM the user owns",
        description=(
            "Work out exactly what one top-up would cost for one of the signed-in user's eSIMs, "
            "and record the choice. THIS DOES NOT TOP ANYTHING UP: it creates no order, moves "
            "no money, adds no data to the SIM and reserves nothing.\n"
            "WHEN: the user has picked a top-up from a get_esim_topup_options list and wants to "
            "know what it costs.\n"
            "Pass the bundle_code of the option they chose, from a get_esim_topup_options result "
            "for THIS eSIM. Never use a code from the general catalogue and never invent one. "
            "The price, the availability and the compatibility are all re-read from the platform "
            "here, so you cannot supply them and must not assume them.\n"
            "AFTER SUCCESS: tell the user the eSIM, the top-up plan and the amount, and say "
            "plainly that nothing was ordered, nothing was charged and no data was added.\n"
            "IMPORTANT: this assistant CANNOT complete a top-up. There is no tool that does, "
            "because the platform cannot yet guarantee that a repeated request would not top the "
            "SIM up twice. After giving the user the quote, tell them the top-up has to be "
            "finished in the eSIM app or on the website. Never say it is reserved, queued, "
            "started or paid for, and never offer to complete it."
        ),
        annotations=ToolAnnotations(
            # Not read-only: it writes a quote into this server's own store, which is durable
            # state for the length of a session even though it is invisible to the platform.
            # Idempotent in the sense that matters: preparing the same choice twice replaces
            # the earlier quote rather than accumulating quotes or charging anything.
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def prepare_esim_topup(
        bundle_code: TopupBundleCodeArg,
        iccid: IccidArg = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "prepare_esim_topup",
            lambda: service.prepare_esim_topup(
                bundle_code=bundle_code, iccid=iccid, locale=locale, currency=currency, ctx=ctx
            ),
        )

    @server.tool(
        name="get_prepared_esim_topup",
        title="Read a prepared eSIM top-up quote",
        description=(
            "Read back a top-up quote you prepared earlier in this conversation: the eSIM, the "
            "plan, the amount and whether it is still valid.\n"
            "Pass the reference from the prepare_esim_topup result. This reads local state only "
            "and never contacts the eSIM platform, so it changes nothing.\n"
            "A quote is short-lived. If the result says it expired or was cancelled, say so "
            "plainly and offer to prepare it again -- and repeat that nothing was ordered, "
            "nothing was charged and no data was added."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def get_prepared_esim_topup(
        quote_id: QuoteIdArg,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "get_prepared_esim_topup", lambda: service.get_prepared_esim_topup(quote_id=quote_id, ctx=ctx)
        )

    @server.tool(
        name="cancel_prepared_esim_topup",
        title="Discard a prepared eSIM top-up quote",
        description=(
            "Throw away a top-up quote you prepared earlier. This discards local information "
            "only.\n"
            "WHEN: the user says they no longer want the top-up you quoted, or wants to start "
            "again.\n"
            "IMPORTANT: there is no order behind a prepared top-up, so nothing is cancelled at "
            "the eSIM platform and nothing is refunded -- there was never a charge. Tell the "
            "user the quote was discarded; never tell them an order was cancelled.\n"
            "This never contacts the eSIM platform."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
    )
    async def cancel_prepared_esim_topup(
        quote_id: QuoteIdArg,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "cancel_prepared_esim_topup", lambda: service.cancel_prepared_esim_topup(quote_id=quote_id, ctx=ctx)
        )

    if not service.execution_enabled:
        # Not registered, not advertised, not callable. In every deployment without the QA
        # flag -- which includes every production one, because production settings refuse to
        # construct with it set -- this is where the top-up capability stops.
        logger.info("esim_topup_execution_tool_not_registered")
        return

    logger.warning(
        "esim_topup_execution_tool_registered",
        # Deliberately loud, and at WARNING. A deployment that can charge a user twice on a
        # retry should say so in its own startup logs.
        extra={"qa_only": True, "idempotent": False},
    )

    @server.tool(
        name="confirm_esim_topup",
        title="Add a prepared top-up to the user's eSIM, paid from their wallet",
        description=(
            "Carry out a top-up you prepared earlier: add the data to the user's eSIM and pay "
            "for it from their wallet balance. THIS SPENDS REAL MONEY and changes a SIM. It "
            "cannot be undone from here and there is no refund tool.\n"
            "CALL THIS AT MOST ONCE PER PREPARED TOP-UP. It is NOT idempotent: the eSIM "
            "platform cannot recognise a second attempt as a repeat, so confirming twice "
            "would charge the user twice and add the data twice. There is no safe retry, in "
            "any circumstance, for any reason.\n"
            "WHEN: only after you have read ALL of the following back to the user from the "
            "prepared top-up -- which eSIM, the plan, the data, the validity, the exact amount "
            "and currency, and that it comes out of their wallet immediately -- and they have "
            'explicitly agreed to that amount: "yes, top it up", "confirm", "go ahead". Never '
            "call it on your own initiative, never to \"check\" something, and never because "
            "the user merely asked what a top-up costs. Wanting a price is not agreeing to pay, "
            "and neither is silence.\n"
            "Pass the quote reference from your own prepare_esim_topup result, and the amount "
            "exactly as that result's confirm_amount field states it. The eSIM, the plan, the "
            "price and the payment method all come from the stored quote, so you cannot supply "
            "or change any of them here -- the amount is checked against the quote and a "
            "mismatch refuses the top-up.\n"
            "Everything is re-checked against the platform first: that the eSIM is still the "
            "user's, that the plan is still offered for it, that the price has not moved, and "
            "that the wallet covers it. Any disagreement refuses, and nothing is charged.\n"
            "AFTER SUCCESS: tell the user plainly that the data was added and paid for from "
            "their wallet, with the plan name and the amount.\n"
            "IF THE RESULT IS UNCLEAR: if it comes back as unknown, NEVER say it succeeded and "
            "NEVER say it failed. Do NOT confirm again and do NOT prepare a replacement -- a "
            "second attempt could charge them twice. Instead find out what actually happened: "
            "check get_my_esims, get_esim_consumption, get_user_profile for the balance, or "
            "get_order_history, and tell the user to contact eSIM support if it is still "
            "unclear."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            # It spends a balance and changes a provisioned SIM, and neither can be undone.
            destructiveHint=True,
            # Stated honestly: this is the one write in this codebase that is NOT idempotent.
            # The server refuses a second attempt, but the platform would not.
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def confirm_esim_topup(
        quote_id: QuoteIdArg,
        confirmed_amount: ConfirmedAmountArg,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "confirm_esim_topup",
            lambda: service.confirm_esim_topup(
                quote_id=quote_id, confirmed_amount=confirmed_amount, ctx=ctx
            ),
        )
