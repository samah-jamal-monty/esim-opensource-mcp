"""Phase 6C MCP tools: adding money to the user's wallet on a Stripe-hosted page.

Three tools: quote an amount, open the page, read what happened. The shape is deliberately
the same as the card checkout, because the money model is the same -- and so are the ways it
can go wrong.

Where the card goes
-------------------
**Nowhere near here.** The user enters their card on Stripe's own hosted page, in their own
browser. This server never renders that page, never proxies it and never receives its
contents; there is no argument on any tool here for a card number, an expiry, a security
code, a cardholder name or a payment token, and no code path that could accept one. An
assistant must therefore never ask for card details -- not to "pass them on", not to "check
them" -- and the tool guidance says so in as many words.

What each tool may take from the model
--------------------------------------
* ``prepare_wallet_topup`` -- an amount, and nothing else. The platform re-validates it
  against its own minimum and its own rolling limits, and the *platform's* answer is what
  the quote records;
* ``create_wallet_topup_checkout`` -- one argument: the reference of a quote this same
  caller prepared. The amount and the currency come from the stored quote, so a hallucinated
  figure has no way into the request;
* ``get_wallet_topup_status`` -- one argument: a payment reference this same caller
  received. There is deliberately no argument through which a model could assert that a
  payment succeeded.

Who is allowed to say a top-up happened
---------------------------------------
**The backend's existing signature-verified Stripe webhook, and nothing else.** Stripe calls
that webhook; the webhook records the payment on the order row and credits the wallet. This
server is a *reader* of that result: it never calls the webhook, never simulates one, never
marks a payment paid, never credits a balance, and holds no Stripe credential with which it
could do any of those. Every "paid" in this module is copied from a status read.

A redirect is not a receipt. The success page the user lands on after paying is a static
page: it credits nothing and confirms nothing, and neither does the user coming back to the
conversation and saying they paid. Only ``get_wallet_topup_status`` can answer the question.

Duplicate protection
--------------------
Not held here. The **platform** recognizes a repeat of the same top-up from the caller's own
pending order row and answers with the page it already opened -- a guarantee that survives a
restart of this process, a redeploy and a move between replicas. What this module keeps is a
copy of the link so a repeated call usually need not leave the process, and an ownership
boundary so a payment reference can only be read by the client that received it. Neither is
load-bearing, and neither is presented to a caller as though it were.

What no tool here ever returns
------------------------------
No access token, refresh token, provider session id, client secret, publishable key, card
detail, backend correlation id or developer message. The results carry the link, the amount,
the currency, the payment reference, the expiry and the state.
"""

from __future__ import annotations

import contextlib
import logging
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from esim_mcp.client.wallet_topup import WalletTopupApiClient, require_topup_reference
from esim_mcp.errors import (
    AuthenticationRequiredError,
    BackendTimeoutError,
    BackendUnavailableError,
    EsimMcpError,
    InvalidInputError,
    RateLimitedError,
    UnsafeCheckoutLinkError,
    WalletTopupAmountInvalidError,
    WalletTopupLimitReachedError,
    WalletTopupOutcomeUnknownError,
    WalletTopupRejectedError,
    WalletTopupStatusUnavailableError,
    WalletTopupUnavailableError,
)
from esim_mcp.models.card import safe_checkout_url
from esim_mcp.models.wallet import decimal_from_number
from esim_mcp.models.wallet_topup import (
    BackendTopupOptions,
    BackendWalletTopupCheckout,
    BackendWalletTopupStatus,
    WalletTopupStatus,
    parse_topup_options,
    parse_wallet_topup_checkout,
    parse_wallet_topup_status,
)
from esim_mcp.purchase.models import money_text
from esim_mcp.purchase.store import QuoteOwner
from esim_mcp.session.identity import ClientIdentityProvider, derive_device_id
from esim_mcp.session.manager import SessionManager
from esim_mcp.settings import Settings
from esim_mcp.tools.guard import guarded
from esim_mcp.tools.purchase_preparation import user_ref_of
from esim_mcp.topup.models import TopupStatus, WalletTopupCheckout, WalletTopupQuote
from esim_mcp.topup.service import (
    WalletTopupCheckoutService,
    WalletTopupQuoteService,
    quote_ref,
    replay_error,
    require_attempts_remaining,
    require_checks_remaining,
    require_usable_topup_quote,
)

logger = logging.getLogger(__name__)

#: Largest amount this server will even parse, mirroring the platform's own parse bound. Not
#: a business limit -- the platform's rolling limits decide what is actually allowed, and
#: they are far lower. This exists so an absurd figure is refused with a sentence rather than
#: crashing the two-decimal formatter on its way to being refused.
MAX_PARSEABLE_AMOUNT = Decimal("1000000")

#: The single sentence a prepared top-up must carry, in every branch.
NOTHING_HAPPENED = "No payment page was opened, nothing was charged and the wallet was not credited."

#: The rule this phase most has to enforce on the *assistant*. Identical in spirit to the
#: card checkout's: this result **is** the confirmation. There is no "the page opened" event,
#: no browser callback and no second tool that reports one. An assistant that waits for one
#: waits forever and withholds a link that already works.
LINK_IS_THE_RESULT = (
    "This result IS the confirmation. Show the user the full checkout_url immediately, in the same reply, exactly "
    "as it appears here and with nothing removed or shortened. Do NOT wait for, ask for or look for any further "
    "confirmation that a payment page 'opened', loaded or was reached: no such signal exists and none is coming. "
    "Whether a browser opens is the user's business and is optional -- the link is the deliverable. Never reply "
    "that you cannot give them a link, and never withhold one you have been given."
)

#: The rule that has to survive into every result in this module, in every branch.
CARD_ENTRY_NOTE = (
    "Card details are entered ONLY on Stripe's own secure hosted page, which opens in the user's browser at the "
    "link above. This assistant never sees, asks for, stores or forwards a card number, an expiry date or a "
    "security code. If the user offers card details in the conversation, tell them not to and point them at the "
    "link instead."
)

#: The single mistake this phase most has to prevent, repeated wherever a state is reported.
REDIRECT_IS_NOT_PROOF = (
    "A browser redirect back from the payment page, a success screen, the user returning to this conversation, "
    "or the user saying they paid are NOT proof of payment and do NOT mean the wallet was credited. Only a "
    "status read from the eSIM platform with get_wallet_topup_status can confirm it -- the platform's own "
    "payment webhook is the one thing that records a top-up and adds the money."
)

#: What to do once a link exists.
CHECKOUT_NEXT_STEP = (
    "Give the user the payment link in full, together with the amount. Say plainly that nothing has been charged "
    "yet, that their balance has not changed, and that they enter their card only on Stripe's secure page, which "
    "the link opens. Never ask them for a card number, an expiry date, a security code or any other card detail, "
    "and never offer to enter one for them. Then wait: do not check the top-up until they say they have paid, or "
    "ask you to check."
)

#: Repeated on a replayed checkout so the assistant does not describe one page as two.
CHECKOUT_REPLAY_NOTE = (
    "This payment link already existed and this is the same link, not a second one. Give it to the user again "
    "rather than treating the repeat as a problem. They are being asked to pay once. Never tell them a new page "
    "was created or that they must pay again."
)

AmountArg = Annotated[
    str,
    Field(
        description=(
            "How much the user wants to add to their wallet, in whole currency units as they "
            "said it -- for example '25' or '25.00'. Ask the user for the amount and pass their "
            "answer; never choose one for them, never round one, and never top up an amount they "
            "have not asked for. The platform checks it against its own minimum and its own "
            "limits, so this is a request, not a decision."
        )
    ),
]

QuoteReferenceArg = Annotated[
    str,
    Field(
        description=(
            "The reference returned by prepare_wallet_topup in this conversation, for the top-up "
            "the user has just explicitly agreed to pay. Never invent, guess or edit one, and "
            "never take one from the user. If you do not have one, or it has expired, prepare "
            "the amount again and ask the user to confirm it before calling this."
        )
    ),
]

PaymentReferenceArg = Annotated[
    str,
    Field(
        description=(
            "The payment reference returned by create_wallet_topup_checkout in this "
            "conversation. Never invent, guess or edit one, and never take one from the user. "
            "There is no argument here for telling this tool that the payment succeeded: "
            "whether it did is read from the eSIM platform and from nowhere else."
        )
    ),
]


class WalletTopupService:
    """Backend-facing behaviour for the three wallet top-up tools."""

    def __init__(
        self,
        settings: Settings,
        topup_client: WalletTopupApiClient,
        session_manager: SessionManager,
        identity_provider: ClientIdentityProvider,
        quotes: WalletTopupQuoteService,
        checkouts: WalletTopupCheckoutService,
    ) -> None:
        self._settings = settings
        self._topups = topup_client
        self._sessions = session_manager
        self._identity_provider = identity_provider
        self._quotes = quotes
        self._checkouts = checkouts

    # ------------------------------------------------------------------ internals

    async def _owner(self, ctx: Any | None) -> tuple[str, QuoteOwner, Any]:
        """Resolve the caller and require a signed-in eSIM user.

        The owner key is built exactly as ``prepare_purchase`` builds it, from the verified
        MCP client identity plus a digest of the authenticated user id, so a top-up prepared
        by one client-and-user pair can only ever be paid by that same pair -- and a payment
        started by one pair can only ever be read by it.
        """
        identity = await self._identity_provider.resolve(ctx)
        device_id = derive_device_id(self._settings.salt_bytes(), identity)
        session = await self._sessions.require_session(identity.session_key)
        return device_id, QuoteOwner(session_key=identity.session_key, user_ref=user_ref_of(session)), session

    def _locale(self, value: str | None) -> str:
        return (value or "").strip() or self._settings.default_locale

    def _currency(self, value: str | None) -> str:
        return ((value or "").strip() or self._settings.default_currency).upper()

    # ------------------------------------------------------------ prepare_wallet_topup

    async def prepare_wallet_topup(
        self,
        *,
        amount: str,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Quote one wallet top-up. Charges nothing and credits nothing."""
        device_id, owner, session = await self._owner(ctx)
        locale_value = self._locale(locale)
        currency_value = self._currency(currency)
        requested = _require_amount(amount)

        options = await self._read_options(
            session_key=owner.session_key, device_id=device_id, locale=locale_value, currency=currency_value
        )
        minimum = decimal_from_number(options.minimum_amount_exclusive)
        maximum = decimal_from_number(options.maximum_amount)
        balance = decimal_from_number(options.current_balance)
        settled_currency = (options.currency or currency_value).strip().upper()

        # The platform's own rules, applied before anything is created. Checking here does
        # not replace the platform's check -- it repeats it, so a user hears "the minimum is
        # X" instead of a refusal after they have already agreed to pay.
        if not options.can_topup:
            raise WalletTopupLimitReachedError(
                _limit_message(options)
                or WalletTopupLimitReachedError.default_message
            )
        if minimum is not None and requested <= minimum:
            raise WalletTopupAmountInvalidError(
                f"The platform will not accept a top-up of {money_text(requested)} {settled_currency}: the "
                f"smallest it takes is more than {money_text(minimum)} {settled_currency}. Tell the user that "
                "and ask them for a different amount."
            )
        if maximum is not None and requested > maximum:
            raise WalletTopupAmountInvalidError(
                f"The platform will not accept a top-up of {money_text(requested)} {settled_currency} right now: "
                f"the most it will take is {money_text(maximum)} {settled_currency}, because of its own daily "
                "limit. Tell the user that figure and ask whether they want to top up that much instead."
            )

        quote = await self._quotes.create(
            owner,
            identity_source=session.identity_source,
            amount=requested,
            currency=settled_currency,
            balance=balance,
            minimum_amount=minimum,
            maximum_amount=maximum,
            locale=locale_value,
        )
        return _prepared_result(quote, options)

    async def _read_options(
        self, *, session_key: str, device_id: str, locale: str, currency: str
    ) -> BackendTopupOptions:
        """Read the platform's own top-up rules for this caller. A read; creates nothing."""

        async def operation(access_token: Any) -> Any:
            return await self._topups.get_options(
                device_id=device_id, access_token=access_token, locale=locale, currency=currency
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
        except AuthenticationRequiredError:
            raise
        except (BackendTimeoutError, BackendUnavailableError):
            logger.warning("wallet_topup_options_unavailable")
            raise WalletTopupUnavailableError() from None
        except InvalidInputError:
            # A 404 from a platform that does not offer the route at all.
            raise WalletTopupUnavailableError(
                "This eSIM platform does not offer assisted wallet top-up, so nothing was charged. Tell the user "
                "they can add money in the eSIM app or on the website instead."
            ) from None

        options = parse_topup_options(data)
        if options is None:
            logger.warning("wallet_topup_options_unreadable")
            raise WalletTopupUnavailableError()
        return options

    # ---------------------------------------------------- create_wallet_topup_checkout

    async def create_wallet_topup_checkout(
        self, *, quote_reference: str, ctx: Any | None = None
    ) -> dict[str, Any]:
        """Open a Stripe-hosted page for a prepared top-up. Charges nothing, credits nothing."""
        device_id, owner, _ = await self._owner(ctx)
        reference = _require_quote_reference(quote_reference)

        # Held across the backend call on purpose: two calls for the same quote must not
        # both reach the platform, whatever order the client sends them in.
        async with self._checkouts.lock(reference):
            quote = await self._quotes.get_record(owner, reference)
            checkout = await self._checkouts.acquire(owner, reference)

            replayed = self._replay_if_terminal(checkout, quote)
            if replayed is not None:
                return replayed

            # Only reached when nothing terminal is recorded, so these gates can never
            # reject a top-up whose payment page already exists.
            require_usable_topup_quote(quote)
            require_attempts_remaining(checkout)

            return await self._open_checkout(quote=quote, checkout=checkout, device_id=device_id)

    def _replay_if_terminal(
        self, checkout: WalletTopupCheckout, quote: WalletTopupQuote
    ) -> dict[str, Any] | None:
        """Hand back the stored answer for a checkout that already reached one."""
        if checkout.status is TopupStatus.OPEN and checkout.result is not None:
            logger.info("wallet_topup_checkout_replayed_locally", extra={"quote_ref": quote_ref(quote.quote_id)})
            return {**checkout.result, "replayed": True, "replay_note": CHECKOUT_REPLAY_NOTE}
        if checkout.status.is_terminal:
            raise replay_error(checkout)
        return None

    async def _open_checkout(
        self,
        *,
        quote: WalletTopupQuote,
        checkout: WalletTopupCheckout,
        device_id: str,
    ) -> dict[str, Any]:
        """Send exactly one checkout request for ``quote`` and interpret the answer."""
        # Counted before the call, so an attempt that dies mid-flight is still an attempt.
        checkout = await self._checkouts.record_attempt(checkout)

        # Refreshed *before* the call rather than replayed after a 401: a replay would be a
        # second POST, and this is not a call to repeat on this server's own initiative.
        session = await self._sessions.ensure_fresh_session(
            checkout.owner_session_key,
            device_id=device_id,
            locale=quote.locale,
            currency=quote.currency,
        )

        logger.info(
            "wallet_topup_checkout_sending",
            extra={"quote_ref": quote_ref(quote.quote_id), "attempt": checkout.attempts},
        )

        try:
            outcome = await self._topups.create_checkout(
                device_id=device_id,
                access_token=session.access_token,
                amount=money_text(quote.amount),
                locale=quote.locale,
                currency=quote.currency,
                read_timeout=self._settings.checkout_read_timeout,
            )
        except (BackendTimeoutError, BackendUnavailableError):
            # A page may or may not have been opened. Nobody was charged either way -- the
            # user has not seen a link, so nothing can have been paid on it. Asking again is
            # safe because the *platform* reuses its own pending order for this top-up.
            await self._checkouts.mark_unresolved(checkout, WalletTopupOutcomeUnknownError())
            logger.warning("wallet_topup_outcome_unknown", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise WalletTopupOutcomeUnknownError(details=_unknown_details(checkout)) from None

        if outcome.is_success:
            return await self._on_checkout_success(data=outcome.data, quote=quote, checkout=checkout)

        raise await self._on_checkout_failure(
            outcome_status=outcome.effective_status,
            hint=outcome.error_hint,
            parsed=outcome.parsed,
            quote=quote,
            checkout=checkout,
        )

    async def _on_checkout_success(
        self, *, data: Any, quote: WalletTopupQuote, checkout: WalletTopupCheckout
    ) -> dict[str, Any]:
        """Interpret a successful envelope. Only a usable, trackable link counts as a page."""
        parsed = parse_wallet_topup_checkout(data)
        if parsed is None:
            await self._checkouts.mark_unresolved(checkout, WalletTopupOutcomeUnknownError())
            logger.warning("wallet_topup_envelope_unreadable", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise WalletTopupOutcomeUnknownError(details=_unknown_details(checkout))

        link = safe_checkout_url(parsed.checkout_url)
        if link is None:
            # Never shown, never repaired. A refused link is a refusal to send the user
            # anywhere, which costs them nothing.
            await self._checkouts.mark_failed(checkout, UnsafeCheckoutLinkError())
            logger.error("wallet_topup_link_refused", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise UnsafeCheckoutLinkError()

        try:
            reference = require_topup_reference(parsed.payment_reference)
        except InvalidInputError:
            # A page this server could not identify afterwards is worse than no page: the
            # user would pay with no way for anyone here to confirm it. So it is not shown.
            error = WalletTopupRejectedError(
                "The eSIM platform did not return a usable reference for this top-up, so the link was not shown "
                "and nothing was charged. Tell the user the top-up could not be started and offer to try again."
            )
            await self._checkouts.mark_failed(checkout, error)
            logger.error("wallet_topup_reference_unusable", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise error from None

        payload = _checkout_result(quote, parsed, link=link, payment_reference=reference)
        await self._checkouts.mark_open(
            checkout,
            result=payload,
            payment_reference=reference,
            locale=quote.locale,
            currency=quote.currency,
        )
        return {**payload, "replayed": bool(parsed.idempotent_replay)}

    async def _on_checkout_failure(
        self,
        *,
        outcome_status: int,
        hint: str,
        parsed: bool,
        quote: WalletTopupQuote,
        checkout: WalletTopupCheckout,
    ) -> EsimMcpError:
        """Classify a non-successful answer, record it, and return the error to raise.

        The error is *returned* rather than raised so the recording above it cannot be
        skipped by an early exit, and so the call site reads as the single place a failed
        checkout leaves this service.
        """
        error, disposition = _classify_checkout_failure(outcome_status, hint, parsed)

        if disposition is _Disposition.TERMINAL:
            await self._checkouts.mark_failed(checkout, error)
        elif disposition is _Disposition.UNRESOLVED:
            await self._checkouts.mark_unresolved(checkout, error)
        # _Disposition.RETRYABLE records nothing: the platform refused before opening
        # anything, so the record stays pending and a later attempt is still allowed.

        logger.info(
            "wallet_topup_checkout_not_opened",
            extra={
                "quote_ref": quote_ref(quote.quote_id),
                "http_status": outcome_status,
                "error_code": error.code,
                "disposition": disposition.value,
                "charged": False,
                "credited": False,
            },
        )
        return error

    # -------------------------------------------------------- get_wallet_topup_status

    async def get_wallet_topup_status(
        self, *, payment_reference: str, ctx: Any | None = None
    ) -> dict[str, Any]:
        """Read what the platform says happened to one top-up this caller started."""
        device_id, owner, _ = await self._owner(ctx)
        reference = require_topup_reference(payment_reference)

        async with self._checkouts.lock(f"topup-payment:{reference}"):
            # Ownership boundary: a reference this client never received is *not found*, not
            # refused, so nothing here can be used to probe for another client's top-ups.
            # The platform scopes its own lookup to the authenticated user as well.
            checkout = await self._checkouts.require_by_reference(owner, reference)

            if checkout.payment_result is not None:
                logger.info("wallet_topup_status_replayed_locally", extra={"quote_ref": quote_ref(checkout.quote_id)})
                return {**checkout.payment_result, "replayed": True}

            require_checks_remaining(checkout)
            checkout = await self._checkouts.record_check(checkout)

            # Asked in the terms the quote was priced in, so an amount read back to the user
            # is the amount they agreed to rather than a server-default conversion of it.
            locale = checkout.locale or self._settings.default_locale
            currency = checkout.currency or self._settings.default_currency

            session = await self._sessions.ensure_fresh_session(
                checkout.owner_session_key, device_id=device_id, locale=locale, currency=currency
            )

            try:
                outcome = await self._topups.get_status(
                    device_id=device_id,
                    access_token=session.access_token,
                    payment_reference=reference,
                    locale=locale,
                    currency=currency,
                )
            except (BackendTimeoutError, BackendUnavailableError):
                # A status read takes nothing and changes nothing, so it is safe to repeat --
                # but never safe to guess at.
                logger.warning("wallet_topup_status_unavailable", extra={"quote_ref": quote_ref(checkout.quote_id)})
                raise WalletTopupStatusUnavailableError() from None

            if not outcome.is_success:
                raise _classify_status_failure(outcome.effective_status)

            return await self._on_status(data=outcome.data, checkout=checkout, owner=owner)

    async def _on_status(
        self, *, data: Any, checkout: WalletTopupCheckout, owner: QuoteOwner
    ) -> dict[str, Any]:
        """Interpret one top-up status. An unreadable answer is never resolved locally."""
        parsed = parse_wallet_topup_status(data)
        status = parsed.topup_status if parsed is not None else None
        if parsed is None or status is None:
            logger.warning("wallet_topup_status_unreadable", extra={"quote_ref": quote_ref(checkout.quote_id)})
            raise WalletTopupStatusUnavailableError()

        payload = _status_result(checkout, parsed, status)

        if status is WalletTopupStatus.PAID:
            # Consumed only here: after the platform confirmed the money arrived, so this
            # quote can never open a second page.
            with contextlib.suppress(EsimMcpError):
                await self._quotes.consume(owner, checkout.quote_id)

        if status.is_terminal:
            await self._checkouts.mark_settled(checkout, payload)

        logger.info(
            "wallet_topup_status_read",
            extra={
                "quote_ref": quote_ref(checkout.quote_id),
                "topup_state": status.value,
                # Read from the platform, never decided here: this server cannot make a
                # payment paid or a balance credited, and the log reflects that as plainly
                # as the result does.
                "charged": status.money_arrived,
                "credited": status.money_arrived,
                "check": checkout.checks,
            },
        )
        return payload


# ------------------------------------------------------------------ outcome classification


class _Disposition(StrEnum):
    """How an unsuccessful checkout answer is remembered.

    ``RETRYABLE`` deliberately records nothing: the platform refused before opening
    anything, so the record stays pending and a later attempt is still allowed.
    """

    TERMINAL = "terminal"
    UNRESOLVED = "unresolved"
    RETRYABLE = "retryable"


#: Backend error labels matched for internal classification only. The platform's envelope has
#: no machine-readable code: ``title`` is a localized string that falls back to the internal
#: error key when no translation exists, so both forms are matched here. Neither is ever
#: forwarded to an MCP client.
_BUSINESS_ERROR_MATCHERS: tuple[tuple[tuple[str, ...], type[EsimMcpError]], ...] = (
    (("invalid_top_up_amount", "top up amount"), WalletTopupAmountInvalidError),
    (("top_up_limit_reached", "maximum number of top-ups"), WalletTopupLimitReachedError),
    (("top_up_amount_limit_exceeded", "maximum daily top-up"), WalletTopupLimitReachedError),
    (("mcp_unsupported_currency", "unsupported currency"), WalletTopupRejectedError),
)


def _classify_checkout_failure(status: int, hint: str, parsed: bool) -> tuple[EsimMcpError, _Disposition]:
    """Map one unsuccessful checkout answer to a typed error and how to remember it.

    Every branch here describes something that did *not* take money: a page that failed to
    open cannot have been paid on, and no balance moves until Stripe tells the platform's
    webhook that it did. That is why none of these carry "may have been charged" wording --
    saying it here would frighten a user for nothing.
    """
    lowered = (hint or "").strip().lower()

    if status in (401, 403):
        return AuthenticationRequiredError(
            "The eSIM session was refused by the platform, so no payment page was opened and nothing was "
            "charged. Ask the user to sign in again, then prepare the top-up again and confirm the amount."
        ), _Disposition.RETRYABLE

    if status == 404:
        return WalletTopupUnavailableError(
            "This eSIM platform does not offer assisted wallet top-up, so no payment page was opened and nothing "
            "was charged. Tell the user they can add money in the eSIM app or on the website instead."
        ), _Disposition.RETRYABLE

    if status == 429:
        return RateLimitedError(
            "The eSIM platform is rate limiting this client, so no payment page was opened and nothing was "
            "charged. Tell the user and offer to try the same top-up again shortly."
        ), _Disposition.RETRYABLE

    if status in (400, 422):
        for needles, error_type in _BUSINESS_ERROR_MATCHERS:
            if any(needle in lowered for needle in needles):
                return error_type(), _Disposition.TERMINAL
        return WalletTopupRejectedError(), _Disposition.TERMINAL

    if status == 503:
        return WalletTopupUnavailableError(), _Disposition.RETRYABLE

    if not parsed and 200 <= status < 300:
        return WalletTopupOutcomeUnknownError(), _Disposition.UNRESOLVED

    if status >= 500:
        return WalletTopupOutcomeUnknownError(), _Disposition.UNRESOLVED

    return WalletTopupRejectedError(), _Disposition.TERMINAL


def _classify_status_failure(status: int) -> EsimMcpError:
    """Map one unsuccessful *status read* to a typed error.

    None of these may claim an outcome. A top-up whose state could not be read is a top-up
    whose state is unknown, and "unknown" is the only honest thing to report.
    """
    if status in (401, 403):
        return AuthenticationRequiredError(
            "The eSIM session was refused by the platform, so this top-up could not be checked. Ask the user to "
            "sign in again, then check the same top-up. Never assume it succeeded or failed in the meantime."
        )
    if status == 429:
        return RateLimitedError(
            "The eSIM platform is rate limiting this client, so the top-up could not be checked. Tell the user "
            "and offer to check the same top-up again shortly. Never guess whether it went through."
        )
    return WalletTopupStatusUnavailableError()


def _unknown_details(checkout: WalletTopupCheckout) -> dict[str, Any]:
    """Safe payload for a checkout whose outcome this server never learned.

    ``retry_safe`` is about *money*, and by that measure retrying is safe twice over: this
    server never touches a card, and the platform recognizes a repeat of the same top-up
    from its own pending order row and answers with the page it already opened. A retry
    therefore cannot produce two payable pages, let alone two charges.
    """
    return {
        "charged": False,
        "credited": False,
        "retry_safe": True,
        "new_quote_safe": False,
        "next_step": "retry_same_checkout",
        "attempts_remaining": checkout.attempts_remaining,
        "retry_note": (
            "Retrying the same prepared top-up cannot charge the user twice: this server never touches a card, "
            "and the platform hands back the page it already opened for this top-up rather than making a second "
            "one. Offer the retry rather than telling the user there is nothing you can do."
        ),
    }


# ------------------------------------------------------------------------ result shaping


def _limit_message(options: BackendTopupOptions) -> str | None:
    """The platform's own explanation of why a top-up is not possible right now."""
    message = (options.message or "").strip()
    return message or None


def _prepared_result(quote: WalletTopupQuote, options: BackendTopupOptions) -> dict[str, Any]:
    """The structured facts a prepared top-up reports.

    ``charged`` and ``credited`` are always present and always ``False``. They are not
    computed: there is no code path in preparation that could make either true, which is
    what makes them safe to state unconditionally.
    """
    result: dict[str, Any] = {
        "status": "prepared",
        "quote_reference": quote.quote_id,
        "expires_at": quote.expires_at.isoformat(),
        "expires_in_seconds": quote.seconds_remaining(),
        "expiry_is_local_bookkeeping": True,
        "expiry_note": (
            "This expiry is internal to this assistant. It is not a deadline the platform set and not a promise "
            "about the amount. Never quote this countdown to the user; if it lapses, simply prepare it again."
        ),
        "amount": money_text(quote.amount),
        "currency": quote.currency,
        "payment_method": "Card",
        "order_created": False,
        "charged": False,
        "credited": False,
        "message": NOTHING_HAPPENED,
        "amount_note": (
            "This is the amount the platform confirmed it will accept, and the amount the card will be charged. "
            "The platform states no fee and no tax on a top-up, so do not mention either and do not tell the "
            "user the final figure may differ."
        ),
        "card_entry_note": CARD_ENTRY_NOTE,
        "next_step": (
            "Read the amount back to the user and ask whether to go ahead. Say plainly that nothing has been "
            "charged and their balance has not changed. Do not ask for card details -- not a number, not an "
            "expiry, not a security code -- and do not offer to enter any. Only if they explicitly agree to pay "
            "that amount, open the secure payment page with create_wallet_topup_checkout and give them the link."
        ),
    }
    if quote.balance is not None:
        result["current_balance"] = money_text(quote.balance)
        result["balance_is_a_snapshot"] = True
    limits = [
        _prune_limit(limit)
        for limit in options.limits
        if limit.code
    ]
    if limits:
        result["platform_limits"] = limits
    if not options.fees:
        result["fees"] = []
        result["fees_note"] = (
            "The platform stated no fee and no tax for this top-up. Do not invent one, and do not warn the user "
            "that the amount might change."
        )
    return result


def _prune_limit(limit: Any) -> dict[str, Any]:
    payload = {
        "code": limit.code,
        "value": str(limit.value) if limit.value is not None else None,
        "remaining": str(limit.remaining) if limit.remaining is not None else None,
        "unit": limit.unit,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _checkout_result(
    quote: WalletTopupQuote,
    checkout: BackendWalletTopupCheckout,
    *,
    link: str,
    payment_reference: str,
) -> dict[str, Any]:
    """The structured facts an open top-up page reports.

    ``charged``, ``paid`` and ``credited`` are ``False`` here unconditionally -- not
    computed, because there is no branch on which opening a page could have taken money or
    moved a balance.
    """
    amount = checkout.amount if checkout.amount is not None else money_text(quote.amount)
    payload: dict[str, Any] = {
        "status": "checkout_ready",
        "checkout_url": link,
        "quote_reference": quote.quote_id,
        "payment_reference": payment_reference,
        "payment_method": "Card",
        "amount": str(amount),
        "currency": (checkout.currency or quote.currency or "").strip().upper(),
        "topup_status": (checkout.topup_status or WalletTopupStatus.PENDING).value,
        "charged": False,
        "paid": False,
        "credited": False,
        "message": (
            "The secure payment link for this top-up is in checkout_url and is ready to give to the user now. "
            "Nothing has been charged and the wallet has not been credited."
        ),
        "link_delivery_note": LINK_IS_THE_RESULT,
        "card_entry_note": CARD_ENTRY_NOTE,
        "next_step": CHECKOUT_NEXT_STEP,
        "proof_note": REDIRECT_IS_NOT_PROOF,
    }
    if checkout.next_action:
        payload["next_action"] = checkout.next_action
    if checkout.expires_at:
        payload["expires_at"] = checkout.expires_at
        payload["expiry_note"] = (
            "This is the platform's own expiry for the payment page. It is the only deadline that is real here "
            "-- never invent one, and never tell the user their amount is held or reserved."
        )
    if checkout.order_id:
        payload["order_id"] = checkout.order_id
        payload["order_state"] = "unpaid"
    return payload


#: What to tell the assistant for each state. Written as instructions rather than as prose
#: about the state, because the difference between "wait" and "start again" is the whole
#: value of reading a status at all.
_STATUS_GUIDANCE: dict[WalletTopupStatus, str] = {
    WalletTopupStatus.PENDING: (
        "The user has not paid yet, and the wallet has not been credited. Give them the payment link again and "
        "ask them to finish on Stripe's secure page. Do not check again until they say they have paid. Never ask "
        "for card details."
    ),
    WalletTopupStatus.PAID: (
        "The payment went through and the platform credited the wallet. Tell the user plainly that the money was "
        "added and give them the amount. If they ask about their balance, read it with get_user_profile rather "
        "than working it out yourself -- the platform is the only source for it."
    ),
    WalletTopupStatus.FAILED: (
        "The payment did not go through, nothing was charged and the wallet was not credited. Tell the user "
        "plainly, and offer to prepare the top-up again and open a new payment page. Never ask them for card "
        "details, and never guess why it failed."
    ),
    WalletTopupStatus.EXPIRED: (
        "The payment page expired before it was paid. Nothing was charged and the wallet was not credited. Tell "
        "the user, and offer to prepare the top-up again and open a new payment page."
    ),
}


def _status_result(
    checkout: WalletTopupCheckout,
    status_payload: BackendWalletTopupStatus,
    status: WalletTopupStatus,
) -> dict[str, Any]:
    """The structured facts one top-up status reports.

    ``paid`` and ``credited`` come from the platform's own status word and from nothing else:
    never from a redirect, a returning user, or a claim made in the conversation. Only the
    platform's signature-verified webhook can move a top-up to paid, and this server only
    ever reads the result of that.
    """
    stored = checkout.result or {}
    payload: dict[str, Any] = {
        "status": status.value.lower(),
        "payment_reference": checkout.payment_reference or "",
        "topup_status": status.value,
        "paid": status.money_arrived,
        "charged": status.money_arrived,
        "credited": status.money_arrived,
        "is_final": status.is_terminal,
        "payment_method": "Card",
        "next_step": _STATUS_GUIDANCE[status],
        "proof_note": REDIRECT_IS_NOT_PROOF,
        "card_entry_note": CARD_ENTRY_NOTE,
    }
    amount = status_payload.amount if status_payload.amount is not None else stored.get("amount")
    if amount is not None:
        payload["amount"] = str(amount)
    currency = status_payload.currency or stored.get("currency")
    if currency:
        payload["currency"] = currency
    if status is WalletTopupStatus.PENDING and stored.get("checkout_url"):
        # Re-offered rather than re-created: the same link the user already has.
        payload["checkout_url"] = stored["checkout_url"]
    if status_payload.expires_at:
        payload["expires_at"] = status_payload.expires_at
    if status_payload.order_id:
        payload["order_id"] = status_payload.order_id
    if status_payload.next_action:
        payload["next_action"] = status_payload.next_action
    if status in (WalletTopupStatus.FAILED, WalletTopupStatus.EXPIRED):
        payload["new_checkout_required"] = True
    return payload


def _require_amount(amount: str) -> Decimal:
    """Parse a caller-supplied amount into an exact :class:`Decimal`.

    A string rather than a float on the wire, and a ``Decimal`` from here on, so the figure
    a user agreed to is the figure that reaches the platform. A value that is not a plain
    positive number is refused rather than coerced: guessing what somebody meant by an
    amount is not a thing to do with money.
    """
    candidate = (amount or "").strip().replace(",", "")
    if not candidate:
        raise WalletTopupAmountInvalidError(
            "No amount was given. Ask the user how much they want to add to their wallet and pass their answer; "
            "never choose an amount for them."
        )
    try:
        value = Decimal(candidate)
    except (InvalidOperation, ValueError):
        raise WalletTopupAmountInvalidError(
            "That is not an amount the platform can read. Ask the user for a plain number, such as 25, and pass "
            "that."
        ) from None
    if not value.is_finite() or value <= 0:
        raise WalletTopupAmountInvalidError(
            "A top-up has to be a positive amount. Ask the user how much they want to add and pass their answer."
        )
    if value > MAX_PARSEABLE_AMOUNT:
        # Refused here rather than left to the platform's own limit, because a Decimal with
        # a huge exponent cannot even be rendered as a two-decimal price: formatting it
        # would raise before anybody got to say no. This is a sanity bound, not a business
        # rule -- the platform's real ceiling is far lower and is enforced below.
        raise WalletTopupAmountInvalidError(
            "That amount is far larger than the platform accepts. Ask the user for a realistic amount, such as "
            "25, and pass that."
        )
    if value.as_tuple().exponent < -2:
        raise WalletTopupAmountInvalidError(
            "The platform settles top-ups to two decimal places. Ask the user for an amount with at most two "
            "decimals, such as 25.50, and pass that."
        )
    return value


def _require_quote_reference(quote_reference: str) -> str:
    candidate = (quote_reference or "").strip()
    if not candidate:
        raise InvalidInputError(
            "A reference from a prepare_wallet_topup result in this conversation is required before a payment "
            "page can be opened. Never invent one, and never start a payment the user has not agreed to."
        )
    return candidate


def register_wallet_topup_tools(server: MCPServer, service: WalletTopupService) -> None:
    """Bind the Phase 6C tools onto an :class:`MCPServer` instance.

    Exactly three tools. There is no ``credit_wallet``, no ``confirm_topup``, no ``capture``
    and no refund -- a model cannot call what does not exist, and this phase adds the ability
    to quote an amount, open one hosted payment page and read its state, and nothing else.
    """

    @server.tool(
        name="prepare_wallet_topup",
        title="Quote adding money to the user's eSIM wallet",
        description=(
            "Check with the eSIM platform that it will accept a given top-up amount for the "
            "signed-in user, and record the choice. THIS CHARGES NOTHING: it opens no payment "
            "page, moves no money and does not change the user's balance.\n"
            "WHEN: the user wants to add money to their wallet -- \"top up my wallet\", \"add "
            "$25 to my balance\", or when a purchase failed for want of balance and they want to "
            "add some.\n"
            "FIRST: the user must be signed in. Check get_login_status and run the normal login "
            "conversation if they are not. Ask the user how much they want to add and pass their "
            "answer -- never choose an amount for them and never round one.\n"
            "The platform's own minimum, its daily limits and the user's current balance are all "
            "read here, so you cannot supply them and must not assume them. If the platform "
            "refuses the amount, it says what it will accept: tell the user that figure and ask "
            "again.\n"
            "AFTER SUCCESS: tell the user the amount and ask whether to go ahead. Say plainly "
            "that nothing has been charged and their balance has not changed. NEVER ask for a "
            "card number, an expiry date or a security code -- nothing here can take a card.\n"
            "Only once they have heard the amount and explicitly agreed to pay it, open the "
            "payment page with create_wallet_topup_checkout."
        ),
        annotations=ToolAnnotations(
            # Not read-only: it writes a quote into this server's own store. It creates
            # nothing at the platform, charges nothing and credits nothing.
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def prepare_wallet_topup(
        amount: AmountArg,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "prepare_wallet_topup",
            lambda: service.prepare_wallet_topup(amount=amount, locale=locale, currency=currency, ctx=ctx),
        )

    @server.tool(
        name="create_wallet_topup_checkout",
        title="Open a secure payment page to add money to the wallet",
        description=(
            "Open the eSIM platform's secure Stripe-hosted page for a top-up the user has "
            "agreed to, and return the payment link. THIS CHARGES NOTHING BY ITSELF: it creates "
            "a payment page, and the user pays on that page or does not. The wallet is credited "
            "only after they pay, by the platform's own payment webhook.\n"
            "WHEN: only after the user has been told the exact amount and has explicitly said "
            'yes to paying it -- "yes, top it up", "go ahead", "open the payment page". Never '
            "call this on your own initiative, and never because the user merely asked what a "
            "top-up would cost. Wanting a quote is not agreeing to pay.\n"
            "FIRST: the amount must already be prepared with prepare_wallet_topup. Read it back "
            "to the user and wait for their answer. If the quote has expired, prepare it again "
            "and get their agreement to the new one -- never start a payment for an amount they "
            "have not heard.\n"
            "Pass only the reference from your own prepare_wallet_topup result. The amount and "
            "the currency come from that stored quote, so you cannot supply or change either.\n"
            "NEVER ask the user for a card number, an expiry date, a security code, a cardholder "
            "name or any other card detail, and never offer to enter one for them. Card details "
            "are entered only on Stripe's own secure page, which the returned link opens in the "
            "user's own browser. This server never sees a card.\n"
            "SAFE TO REPEAT: calling this twice for the same prepared top-up returns the same "
            "payment link and never opens a second page, so the user is never asked to pay "
            "twice.\n"
            "AFTER SUCCESS: the result contains checkout_url. Give the user that link in full, "
            "in your very next reply, with the amount, and say plainly that nothing has been "
            "charged yet and their balance has not changed. The result itself is the "
            "confirmation: there is NO further signal that a page 'opened', so never wait for "
            "one, never ask the user whether a page opened, and never answer that you cannot "
            "give them a link. Then wait -- use get_wallet_topup_status when they say they have "
            "paid or ask you to check.\n"
            "IF NO LINK COMES BACK: that is a lost answer, not a refusal, and nothing was "
            "charged. Offer to try the same prepared top-up again straight away -- the platform "
            "hands back the page it already made rather than opening a second one."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def create_wallet_topup_checkout(
        quote_reference: QuoteReferenceArg,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "create_wallet_topup_checkout",
            lambda: service.create_wallet_topup_checkout(quote_reference=quote_reference, ctx=ctx),
        )

    @server.tool(
        name="get_wallet_topup_status",
        title="Check what happened to a wallet top-up payment",
        description=(
            "Ask the eSIM platform what happened to a top-up you started with "
            "create_wallet_topup_checkout. This is the ONLY way to know whether the payment went "
            "through and whether the wallet was credited: the platform's own payment webhook "
            "records the payment and adds the money, and this tool reads the result of that. "
            "Nothing here can make a payment succeed or credit a balance.\n"
            "WHEN: the user says they have paid, or asks you to check. Do not call it on a loop "
            "and do not keep checking on your own -- one check per request, and only when there "
            "is a reason to.\n"
            "NEVER treat any of these as proof: the user returning to this conversation, a "
            "browser redirect, a success screen on the payment page, or the user simply saying "
            "it worked. Only this tool's answer counts, and there is no argument here through "
            "which you could tell it that a payment succeeded.\n"
            "The answer means exactly what it says:\n"
            "- not paid yet: give the user the link again and wait; do not ask for card details.\n"
            "- paid: the money arrived and the platform credited the wallet. Say so plainly with "
            "the amount. Read the balance with get_user_profile rather than working it out.\n"
            "- failed or expired: nothing was charged and nothing was credited. Say so plainly "
            "and offer to prepare the top-up again.\n"
            "IF IT CANNOT BE READ: never say it succeeded and never say it failed. Tell the user "
            "it could not be checked and offer to check the same top-up again shortly."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def get_wallet_topup_status(
        payment_reference: PaymentReferenceArg,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "get_wallet_topup_status",
            lambda: service.get_wallet_topup_status(payment_reference=payment_reference, ctx=ctx),
        )
