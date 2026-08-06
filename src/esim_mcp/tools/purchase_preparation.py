"""Phase 3 MCP tools: safe purchase preparation.

Contract enforced by this layer
-------------------------------
* **Nothing is bought.** No tool here creates a backend order, starts a payment, debits a
  wallet, contacts a payment provider or provisions an eSIM. There is no execution tool in
  this phase, so there is nothing for the model to escalate to; every result states
  ``order_created: false`` and ``charged: false`` explicitly, in every branch.
* **Only three safe reads are used**: ``GET /bundles/{bundle_code}`` for the plan,
  ``GET /wallet/user_wallet_by_user`` for the balance, and the session layer's existing
  ``GET /auth/user-info`` refresh path. The transport itself refuses every order, payment,
  voucher, top-up and provisioning route (see :mod:`esim_mcp.client.base`).
* **Nothing priced is taken from the caller.** Price, wallet balance, tax, discount and
  availability are re-read from the platform at preparation time. The tool signature has no
  argument through which any of them could be supplied, so a model that hallucinates a price
  cannot get it into a quote.
* **No secret is accepted or returned.** No token, card detail, user id or device id appears
  in an argument, a result or a stored quote.

:class:`PurchasePreparationService` holds the behaviour and is directly unit-testable;
:func:`register_purchase_preparation_tools` is the thin MCP binding.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field, SecretStr

from esim_mcp.catalog.resolution import CountryResolver, RegionResolver
from esim_mcp.client.catalog import CatalogApiClient
from esim_mcp.client.wallet import WalletApiClient
from esim_mcp.errors import (
    BundleUnavailableError,
    EsimMcpError,
    InvalidInputError,
    WalletUnavailableError,
)
from esim_mcp.models.catalog import Bundle
from esim_mcp.models.wallet import decimal_from_number
from esim_mcp.purchase.models import (
    PaymentMethod,
    PurchaseQuote,
    QuotedBundle,
    QuotedPrice,
    QuotedWallet,
    QuoteStatus,
    SearchContext,
    SearchContextKind,
    money_text,
)
from esim_mcp.purchase.service import PurchaseQuoteService
from esim_mcp.purchase.store import QuoteOwner
from esim_mcp.purchase.validation import (
    evaluate_wallet,
    parse_payment_method,
    require_positive_price,
)
from esim_mcp.session.identity import ClientIdentity, ClientIdentityProvider, derive_device_id
from esim_mcp.session.manager import SessionManager
from esim_mcp.session.models import UserSession
from esim_mcp.settings import Settings
from esim_mcp.tools.guard import guarded

logger = logging.getLogger(__name__)

_USER_REF_NAMESPACE = b"esim-mcp/quote-owner/v1"

#: Wording every priced result repeats. The catalogue amount is a display amount, and that
#: is the whole of what this server knows: the platform states no tax, no surcharge and no
#: adjustment at this stage, so neither does this. Saying "the final amount may differ" would
#: be inventing a claim out of the platform's silence -- and on the card path it is simply
#: wrong, because create_card_checkout returns the exact settled amount.
PRICE_DISCLAIMER = (
    "This is the platform's price for the plan. Give the user this figure as it stands. Do "
    "not attach caveats the platform never made: no warning about tax, no suggestion that "
    "the amount is provisional or approximate, and no promise that it will be settled or "
    "recalculated later. The platform priced the plan and sent the price."
)

#: The single sentence that must survive into every result, in every branch.
NOTHING_HAPPENED = "No order was created and nothing was charged."

#: What a Card quote can and cannot say. Kept out of the tool *description* on purpose: the
#: description advertises what *this* tool does, and opening a payment page is a different
#: tool's job -- one the user has to agree to first.
CARD_NOTE = (
    "Card preparation records the choice and the amount only. No payment page exists yet and "
    "none is created here. Never ask the user for card details, and never invent a payment "
    "link: the link comes from create_card_checkout and from nowhere else."
)

#: What to do after a Card quote. Deliberately a separate sentence from the Wallet one: the
#: two payment methods diverge completely from here on, and a single shared instruction would
#: have to be vague about both.
CARD_NEXT_STEP = (
    "Read the plan, the amount and the payment method back to the user and tell them plainly that no payment page "
    "exists yet and nothing was charged. Do not say the plan is reserved or held. Do not ask for card details -- "
    "not a number, not an expiry, not a security code -- and do not offer to enter any. If the user explicitly "
    "agrees to pay that amount by card, open the platform's secure payment page with create_card_checkout and give "
    "them the link it returns."
)

WALLET_INSUFFICIENT_NOTE = (
    "The wallet balance does not cover this plan, so it cannot proceed by wallet. Tell the user the shortfall "
    "and offer either adding funds outside this assistant or preparing the same plan for card payment instead."
)

BundleCodeArg = Annotated[
    str,
    Field(
        description=(
            "The bundle_code of the plan the user chose, taken verbatim from a catalogue result "
            "you already have in this conversation. Never invent, guess or edit one, never "
            "derive one from a plan's display name, and never ask the user to read one out. If "
            "you do not have the code for the plan they mean, search the destination again and "
            "use the code from the option they picked."
        )
    ),
]
PaymentMethodArg = Annotated[
    str,
    Field(
        description=(
            "How the user wants to pay: 'Wallet' to use their eSIM account balance, or 'Card'. "
            "Ask the user which one they want and pass their answer -- never choose on their "
            "behalf, and never default to one."
        )
    ),
]
CountryContextArg = Annotated[
    str | None,
    Field(
        description=(
            "Optional. The country whose plan list the user chose from, in their own words, when "
            "the selection came out of a country search. Preserves the destination context; it "
            "never affects the price."
        )
    ),
]
RegionContextArg = Annotated[
    str | None,
    Field(
        description=(
            "Optional. The region whose plan list the user chose from, when the selection came "
            "out of a region search. Preserves the destination context; it never affects the price."
        )
    ),
]
QuoteIdArg = Annotated[
    str,
    Field(
        description=(
            "The quote reference returned by prepare_purchase in this conversation. Never invent, guess or edit one."
        )
    ),
]
LocaleArg = Annotated[
    str | None,
    Field(description="Optional language tag for platform text, e.g. 'en'. Omit to use the server default."),
]
CurrencyArg = Annotated[
    str | None,
    Field(description="Optional ISO-4217 currency code, e.g. 'USD'. Omit to use the server default."),
]


class PurchasePreparationService:
    """Backend-facing behaviour for the three purchase-preparation tools."""

    def __init__(
        self,
        settings: Settings,
        catalog_client: CatalogApiClient,
        wallet_client: WalletApiClient,
        session_manager: SessionManager,
        identity_provider: ClientIdentityProvider,
        quotes: PurchaseQuoteService,
    ) -> None:
        self._settings = settings
        self._catalog = catalog_client
        self._wallet = wallet_client
        self._sessions = session_manager
        self._identity_provider = identity_provider
        self._quotes = quotes
        self._countries = CountryResolver(catalog_client)
        self._regions = RegionResolver(catalog_client)

    # ------------------------------------------------------------------ internals

    async def _caller(self, ctx: Any | None) -> tuple[ClientIdentity, str]:
        identity = await self._identity_provider.resolve(ctx)
        return identity, derive_device_id(self._settings.salt_bytes(), identity)

    async def _owner(self, ctx: Any | None) -> tuple[ClientIdentity, str, UserSession, QuoteOwner]:
        """Resolve the caller and require a signed-in eSIM user.

        Raises :class:`~esim_mcp.errors.AuthenticationRequiredError` when nobody is signed in;
        preparation is the first thing in this server that genuinely needs an account.
        """
        identity, device_id = await self._caller(ctx)
        session = await self._sessions.require_session(identity.session_key)
        owner = QuoteOwner(session_key=identity.session_key, user_ref=user_ref_of(session))
        return identity, device_id, session, owner

    def _locale(self, locale: str | None) -> str:
        return (locale or self._settings.default_locale).strip()

    def _currency(self, currency: str | None) -> str:
        return (currency or self._settings.default_currency).strip().upper()

    async def _authoritative_bundle(
        self,
        bundle_code: str,
        *,
        device_id: str,
        locale: str,
        currency: str,
    ) -> Bundle:
        """Re-read the plan from the platform and refuse it unless it is still offerable.

        Always a fresh read, never a cached or caller-supplied record: the price, the currency
        and the availability flag in a quote have to be the platform's current values.
        """
        bundle = await self._catalog.get_bundle_details(
            bundle_code, device_id=device_id, locale=locale, currency=currency
        )
        if not bundle.is_available:
            logger.info("quote_rejected_inactive_bundle")
            raise BundleUnavailableError()
        return bundle

    async def _wallet_snapshot(
        self,
        *,
        session_key: str,
        device_id: str,
        locale: str,
        currency: str,
        price: Any,
    ) -> QuotedWallet:
        """Read the authoritative balance and compare it with ``price`` in ``Decimal``."""

        async def operation(access_token: SecretStr) -> Any:
            return await self._wallet.get_user_wallet(
                device_id=device_id,
                access_token=access_token,
                locale=locale,
                currency=currency,
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

    async def _search_context(
        self,
        *,
        country: str | None,
        region: str | None,
        device_id: str,
        locale: str,
    ) -> tuple[SearchContext, str | None]:
        """Resolve the caller's destination context against the platform's own lists.

        The context is only ever recorded from an authoritative record, never from the words
        the caller passed. When it cannot be confirmed -- nothing supplied, or a name the
        catalogue does not know -- the quote is still created and the context is marked
        incomplete, with a note the assistant can pass on. Losing context costs the execution
        phase a lookup; failing the quote would cost the user their choice.
        """
        if country and region:
            raise InvalidInputError(
                "Pass either the country or the region the plan was chosen from, not both. Use whichever search "
                "produced the list the user picked from."
            )
        if country:
            try:
                resolved = await self._countries.resolve(country, device_id=device_id, locale=locale)
            except EsimMcpError:
                logger.info("quote_country_context_unresolved")
                return SearchContext(kind=SearchContextKind.NONE, is_complete=False), (
                    "The destination context could not be confirmed against the platform's country list, so it "
                    "was not recorded. The plan details themselves are authoritative."
                )
            return (
                SearchContext(
                    kind=SearchContextKind.COUNTRY,
                    country_name=resolved.name,
                    country_iso3=resolved.iso3,
                    is_complete=bool(resolved.iso3),
                ),
                None,
            )
        if region:
            try:
                resolved_region = await self._regions.resolve(region, device_id=device_id, locale=locale)
            except EsimMcpError:
                logger.info("quote_region_context_unresolved")
                return SearchContext(kind=SearchContextKind.NONE, is_complete=False), (
                    "The destination context could not be confirmed against the platform's region list, so it "
                    "was not recorded. The plan details themselves are authoritative."
                )
            return (
                SearchContext(
                    kind=SearchContextKind.REGION,
                    region_name=resolved_region.name,
                    region_code=resolved_region.code,
                    is_complete=bool(resolved_region.code),
                ),
                None,
            )
        return SearchContext(kind=SearchContextKind.NONE, is_complete=False), None

    # ---------------------------------------------------------------------- tools

    async def prepare_purchase(
        self,
        *,
        bundle_code: str,
        payment_method: str,
        country: str | None = None,
        region: str | None = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Prepare a local quote for a plan the user selected. Creates no order and charges nothing."""
        method = parse_payment_method(payment_method)
        _, device_id, session, owner = await self._owner(ctx)
        locale_value = self._locale(locale)
        currency_value = self._currency(currency)

        bundle = await self._authoritative_bundle(
            bundle_code, device_id=device_id, locale=locale_value, currency=currency_value
        )
        amount = require_positive_price(decimal_from_number(bundle.price), bundle.currency_code or currency_value)
        price = QuotedPrice(displayed_amount=amount, currency=bundle.currency_code or currency_value)

        wallet: QuotedWallet | None = None
        if method is PaymentMethod.WALLET:
            wallet = await self._wallet_snapshot(
                session_key=owner.session_key,
                device_id=device_id,
                locale=locale_value,
                currency=price.currency,
                price=amount,
            )

        context, context_note = await self._search_context(
            country=country, region=region, device_id=device_id, locale=locale_value
        )

        quote = await self._quotes.create(
            owner,
            identity_source=session.identity_source,
            bundle=_quoted_bundle(bundle),
            payment_method=method,
            price=price,
            wallet=wallet,
            search_context=context,
            locale=locale_value,
        )
        return _prepared_result(quote, context_note=context_note)

    async def get_prepared_purchase(self, *, quote_id: str, ctx: Any | None = None) -> dict[str, Any]:
        """Read back one of this caller's own quotes. Never touches the backend."""
        _, _, _, owner = await self._owner(ctx)
        quote = await self._quotes.get_record(owner, _require_quote_id(quote_id))
        status = quote.effective_status()
        if status is not QuoteStatus.ACTIVE:
            return _inactive_result(quote, status)
        return _prepared_result(quote, status_value="prepared")

    async def cancel_prepared_purchase(self, *, quote_id: str, ctx: Any | None = None) -> dict[str, Any]:
        """Cancel one of this caller's own quotes. Local only -- there is no backend order to cancel."""
        _, _, _, owner = await self._owner(ctx)
        quote = await self._quotes.cancel(owner, _require_quote_id(quote_id))
        return {
            "status": "cancelled",
            "quote_id": quote.quote_id,
            "order_cancelled": False,
            "charged": False,
            "bundle": {"name": quote.bundle.name},
            "message": "The local quote was cancelled. No backend order existed.",
            "note": (
                "Nothing was refunded or reversed because nothing was ever ordered or charged. Confirm the "
                "cancellation plainly and offer to look at other plans."
            ),
        }


# ------------------------------------------------------------------------ result shaping


def _quoted_bundle(bundle: Bundle) -> QuotedBundle:
    """Copy the authoritative plan facts into the quote's own typed snapshot."""
    return QuotedBundle(
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


def _prepared_result(
    quote: PurchaseQuote,
    *,
    context_note: str | None = None,
    status_value: str = "prepared",
) -> dict[str, Any]:
    """The structured facts a prepared quote reports.

    ``order_created`` and ``charged`` are always present and always ``False``: they are not
    computed, because there is no code path in this phase that could make either true.
    """
    result: dict[str, Any] = {
        "status": status_value,
        "quote_id": quote.quote_id,
        "expires_at": quote.expires_at.isoformat(),
        "expires_in_seconds": quote.seconds_remaining(),
        # This expiry is this server's own bookkeeping, not a promise the platform made. It
        # says how long *this server* will keep the quote usable; it holds no price, reserves
        # no plan and obliges the platform to nothing. Telling the user "your price is valid
        # for about five minutes" would turn an internal cache lifetime into a commitment
        # nobody made, so the model is told plainly not to.
        "expiry_is_local_bookkeeping": True,
        "expiry_note": (
            "This expiry is internal to this assistant and is not a price guarantee. Never tell the user the "
            "price is held, locked, reserved or valid for some number of minutes, and never quote this countdown "
            "to them. If the quote does lapse before they decide, simply prepare the plan again."
        ),
        "bundle": {
            "bundle_code": quote.bundle.bundle_code,
            "name": quote.bundle.name,
            "data": quote.bundle.data_display,
            "unlimited": quote.bundle.unlimited,
            "validity": quote.bundle.validity_display,
        },
        "payment_method": quote.payment_method.value,
        "pricing": {
            "displayed_amount": money_text(quote.price.displayed_amount),
            "currency": quote.price.currency,
            "final_amount_confirmed": False,
            "tax_included_confirmed": False,
        },
        "order_created": False,
        "charged": False,
        "message": NOTHING_HAPPENED,
        "price_note": PRICE_DISCLAIMER,
    }
    if quote.bundle.plan_type:
        result["bundle"]["plan_type"] = quote.bundle.plan_type
    if quote.bundle.activation_policy:
        result["bundle"]["activation_policy"] = quote.bundle.activation_policy
    if quote.bundle.countries_count:
        result["bundle"]["countries_count"] = quote.bundle.countries_count

    if quote.wallet is not None:
        wallet: dict[str, Any] = {
            "balance": money_text(quote.wallet.balance),
            "currency": quote.wallet.currency,
            "sufficient": quote.wallet.sufficient,
        }
        if quote.wallet.estimated_remaining_balance is not None:
            wallet["estimated_remaining_balance"] = money_text(quote.wallet.estimated_remaining_balance)
        if quote.wallet.shortfall is not None:
            wallet["shortfall"] = money_text(quote.wallet.shortfall)
        wallet["balance_is_a_snapshot"] = True
        result["wallet"] = wallet
        if not quote.wallet.sufficient:
            result["can_proceed_with_wallet"] = False
            result["precondition"] = WALLET_INSUFFICIENT_NOTE
    if quote.payment_method is PaymentMethod.CARD:
        result["card"] = {"payment_link": None, "payment_intent_created": False}
        result["card_note"] = CARD_NOTE

    context = quote.search_context
    result["search_context"] = {
        "kind": context.kind.value,
        "is_complete": context.is_complete,
        **({"country": context.country_name} if context.country_name else {}),
        **({"region": context.region_name} if context.region_name else {}),
    }
    if context_note:
        result["search_context_note"] = context_note

    result["next_step"] = (
        CARD_NEXT_STEP
        if quote.payment_method is PaymentMethod.CARD
        else (
            "Read the plan, the amount and the payment method back to the user and tell them plainly that no order "
            "was created and nothing was charged. Completing a purchase is not available in this version, so do not "
            "offer to do it, do not say the plan is reserved or held, and do not ask for card details. If they say "
            "yes, confirm, or pay now, explain that purchase execution arrives in a later phase and that the quote "
            "is all this version can produce."
        )
    )
    return result


def _inactive_result(quote: PurchaseQuote, status: QuoteStatus) -> dict[str, Any]:
    """Honest reporting for a quote that is no longer usable."""
    reasons = {
        QuoteStatus.EXPIRED: (
            "This quote has expired, so its price and balance are no longer current. Offer to prepare the same "
            "plan again."
        ),
        QuoteStatus.CANCELLED: (
            "This quote was cancelled -- either explicitly, or because it was replaced by a newer quote for the "
            "same plan and payment method, or because the user signed out. Offer to prepare it again."
        ),
        QuoteStatus.CONSUMED: "This quote has already been used and cannot be read again.",
    }
    return {
        "status": status.value,
        "quote_id": quote.quote_id,
        "expires_at": quote.expires_at.isoformat(),
        "bundle": {"name": quote.bundle.name},
        "payment_method": quote.payment_method.value,
        "order_created": False,
        "charged": False,
        "message": NOTHING_HAPPENED,
        "note": reasons.get(status, "This quote is no longer usable."),
    }


def _require_quote_id(quote_id: str) -> str:
    candidate = (quote_id or "").strip()
    if not candidate:
        raise InvalidInputError(
            "A quote reference from a prepare_purchase result in this conversation is required. Never invent one."
        )
    return candidate


def user_ref_of(session: UserSession) -> str:
    """Stable, non-reversible reference to the authenticated eSIM user.

    Digested rather than stored raw so a quote record holds no backend user id, and so the
    same MCP client signed in as a different user gets a different owner key.

    Public because the execution phase has to derive *exactly* this value: a quote may only
    be confirmed by the client-and-user pair that prepared it, and two independent
    implementations of the owner key would be two chances to get that wrong.
    """
    digest = hashlib.sha256()
    digest.update(_USER_REF_NAMESPACE)
    digest.update((session.user_id or session.session_key).encode("utf-8"))
    return digest.hexdigest()


def register_purchase_preparation_tools(server: MCPServer, service: PurchasePreparationService) -> None:
    """Bind the Phase 3 tools onto an :class:`MCPServer` instance.

    Deliberately three tools and no more. None of them can complete a purchase of either kind:
    paying lives in separate tools, registered elsewhere, which the model has to choose
    deliberately after the user has agreed to an amount. A tool that both quotes and pays
    would make that agreement skippable.
    """

    @server.tool(
        name="prepare_purchase",
        title="Prepare an eSIM plan for purchase",
        description=(
            "Prepare a signed-in user's chosen plan for buying, and report exactly what it would "
            "cost. This does NOT buy anything: it creates no order, moves no money, and reserves "
            "nothing.\n"
            "WHEN: a signed-in user has picked a real plan from a list you showed and wants to go "
            'ahead -- "I want the second one", "prepare the cheapest France plan".\n'
            "FIRST: the user must be signed in. Check get_login_status and run the normal login "
            "conversation if they are not. Then ask whether they want to use their wallet balance "
            "or a card, and pass their answer -- never pick for them.\n"
            "Pass the bundle_code of the plan they chose, taken from the catalogue result you "
            "already have. Never invent a code and never ask the user for one. If it is unclear "
            "which plan they mean, ask them which one before calling this.\n"
            "The plan's price, availability and the wallet balance are all re-read from the "
            "platform here, so you cannot supply them and must not assume them.\n"
            "AFTER SUCCESS: tell the user the plan, the amount and the payment method, and say "
            "plainly that no order was created and nothing was charged. Never say the plan is "
            "reserved, held or bought. Do not ask for card details. Do not call this again for the "
            "same choice -- if you do, the earlier quote is replaced.\n"
            "Preparing is not paying, and this tool cannot take a payment of either kind. Only "
            "once the user has heard the amount and explicitly agreed to it does paying begin: "
            "confirm_purchase for a Wallet quote, create_card_checkout for a Card quote. Never "
            "start either on your own initiative."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        ),
    )
    async def prepare_purchase(
        bundle_code: BundleCodeArg,
        payment_method: PaymentMethodArg,
        country: CountryContextArg = None,
        region: RegionContextArg = None,
        locale: LocaleArg = None,
        currency: CurrencyArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "prepare_purchase",
            lambda: service.prepare_purchase(
                bundle_code=bundle_code,
                payment_method=payment_method,
                country=country,
                region=region,
                locale=locale,
                currency=currency,
                ctx=ctx,
            ),
        )

    @server.tool(
        name="get_prepared_purchase",
        title="Read a prepared eSIM quote",
        description=(
            "Read back a quote you prepared earlier in this conversation: the plan, the amount, the "
            "payment method and whether it is still valid.\n"
            'WHEN: the user asks about what you prepared -- "show me my prepared purchase", "what '
            'was that going to cost".\n'
            "Pass the quote reference from the prepare_purchase result. This reads local state only "
            "and never contacts the eSIM platform, so it changes nothing.\n"
            "A quote is short-lived. If the result says it expired or was cancelled, say so plainly "
            "and offer to prepare the plan again -- and repeat that no order was created and nothing "
            "was charged."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def get_prepared_purchase(
        quote_id: QuoteIdArg,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded("get_prepared_purchase", lambda: service.get_prepared_purchase(quote_id=quote_id, ctx=ctx))

    @server.tool(
        name="cancel_prepared_purchase",
        title="Cancel a prepared eSIM quote",
        description=(
            "Throw away a quote you prepared earlier. This discards local information only.\n"
            "WHEN: the user says they do not want the plan you prepared, or wants to start again.\n"
            "IMPORTANT: there is no order behind a prepared quote, so nothing is cancelled at the "
            "eSIM platform and nothing is refunded or reversed -- there was never a charge. Tell the "
            "user the prepared quote was discarded; never tell them an order was cancelled.\n"
            "This never contacts the eSIM platform."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
    )
    async def cancel_prepared_purchase(
        quote_id: QuoteIdArg,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "cancel_prepared_purchase", lambda: service.cancel_prepared_purchase(quote_id=quote_id, ctx=ctx)
        )
