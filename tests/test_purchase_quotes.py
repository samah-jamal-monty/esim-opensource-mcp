"""The quote domain: models, money arithmetic, the store and the lifecycle service.

These tests drive :mod:`esim_mcp.purchase` directly, with no MCP layer and no HTTP at all.
That separation is the point: a quote's ownership, expiry, limit and money rules have to hold
whatever calls them, including whatever the execution phase turns out to look like.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from esim_mcp.errors import (
    InsufficientWalletBalanceError,
    InvalidInputError,
    QuoteCancelledError,
    QuoteConsumedError,
    QuoteExpiredError,
    QuoteNotFoundError,
    QuoteNotOwnedError,
    TooManyActiveQuotesError,
    UnsupportedPaymentMethodError,
)
from esim_mcp.purchase.models import (
    QUOTE_SCHEMA_VERSION,
    PaymentMethod,
    PurchaseQuote,
    QuotedBundle,
    QuotedPrice,
    QuoteStatus,
    SearchContext,
    SearchContextKind,
    money_text,
    utc_now,
)
from esim_mcp.purchase.service import PurchaseQuoteService
from esim_mcp.purchase.store import InMemoryPurchaseQuoteStore, PurchaseQuoteStore, QuoteOwner
from esim_mcp.purchase.validation import (
    evaluate_wallet,
    new_quote_id,
    parse_payment_method,
    require_positive_price,
    require_usable_quote,
    require_wallet_sufficient,
)
from esim_mcp.settings import Settings

OWNER_A = QuoteOwner(session_key="a" * 64, user_ref="1" * 64)
OWNER_B = QuoteOwner(session_key="b" * 64, user_ref="2" * 64)
#: Same MCP client, different signed-in user -- the case a one-part owner key would miss.
OWNER_A_OTHER_USER = QuoteOwner(session_key="a" * 64, user_ref="9" * 64)

BUNDLE = QuotedBundle(
    bundle_code="aaaaaaaa-0000-4000-8000-000000000001",
    name="France 10GB",
    data_display="10 GB",
    unlimited=False,
    validity_display="30 Days",
    validity_days=30,
    plan_type="Data only",
    countries_count=7,
)
PRICE = QuotedPrice(displayed_amount=Decimal("8.06"), currency="USD")


async def create(
    service: PurchaseQuoteService,
    owner: QuoteOwner = OWNER_A,
    *,
    bundle: QuotedBundle = BUNDLE,
    payment_method: PaymentMethod = PaymentMethod.CARD,
    **kwargs: object,
) -> PurchaseQuote:
    return await service.create(
        owner,
        identity_source="test",
        bundle=bundle,
        payment_method=payment_method,
        price=PRICE,
        wallet=None,
        search_context=SearchContext(),
        locale="en",
        **kwargs,  # type: ignore[arg-type]
    )


def other_bundle(code: str) -> QuotedBundle:
    return BUNDLE.model_copy(update={"bundle_code": code})


# ------------------------------------------------------------------------ quote identifiers


def test_quote_ids_are_random_and_unguessable() -> None:
    ids = {new_quote_id() for _ in range(1000)}

    assert len(ids) == 1000, "quote ids collided, so they are not random"
    assert all(len(value) >= 40 for value in ids)


def test_a_quote_id_encodes_nothing_about_its_owner_or_its_plan() -> None:
    """The id is shown to the model and echoed back, so it must disclose nothing."""
    secrets_that_must_not_appear = (
        "person@example.com",
        "+96171123467",
        OWNER_A.session_key,
        OWNER_A.user_ref,
        BUNDLE.bundle_code,
        "France",
    )

    for _ in range(200):
        quote_id = new_quote_id()
        for secret in secrets_that_must_not_appear:
            assert secret not in quote_id
            assert secret[:8] not in quote_id


def test_quote_ids_are_not_sequential() -> None:
    """Two ids made back to back must share no meaningful prefix."""
    first, second = new_quote_id(), new_quote_id()

    assert first != second
    assert first[:8] != second[:8]


# ------------------------------------------------------------------------ money arithmetic


@pytest.mark.parametrize(
    ("balance", "price", "sufficient", "remaining"),
    [
        ("20.00", "8.06", True, "11.94"),
        ("8.06", "8.06", True, "0.00"),
        ("8.05", "8.06", False, None),
        ("0.00", "8.06", False, None),
        ("0.30", "0.10", True, "0.20"),
    ],
)
def test_wallet_arithmetic_is_exact(balance: str, price: str, sufficient: bool, remaining: str | None) -> None:
    """Every figure a user hears comes from :class:`Decimal`, never from a float."""
    snapshot = evaluate_wallet(balance=Decimal(balance), currency="USD", price=Decimal(price))

    assert snapshot.sufficient is sufficient
    if remaining is None:
        assert snapshot.estimated_remaining_balance is None
    else:
        assert money_text(snapshot.estimated_remaining_balance) == remaining


def test_the_classic_float_case_does_not_drift() -> None:
    """The same subtraction in binary floating point is wrong; in Decimal it is exact."""
    assert 0.30 - 0.10 != 0.20  # 0.19999999999999998 -- what a float wallet would report

    snapshot = evaluate_wallet(balance=Decimal("0.30"), currency="USD", price=Decimal("0.10"))

    assert snapshot.estimated_remaining_balance == Decimal("0.20")
    assert money_text(snapshot.estimated_remaining_balance) == "0.20"


def test_a_shortfall_is_reported_instead_of_a_negative_remaining_balance() -> None:
    snapshot = evaluate_wallet(balance=Decimal("2.00"), currency="USD", price=Decimal("8.06"))

    assert snapshot.sufficient is False
    assert snapshot.estimated_remaining_balance is None
    assert money_text(snapshot.shortfall) == "6.06"


def test_wallet_arithmetic_uses_decimal_types_end_to_end() -> None:
    snapshot = evaluate_wallet(balance=Decimal("20.00"), currency="USD", price=Decimal("8.06"))

    assert isinstance(snapshot.balance, Decimal)
    assert isinstance(snapshot.estimated_remaining_balance, Decimal)
    assert not isinstance(snapshot.balance, float)


@pytest.mark.parametrize("amount", [None, Decimal("-1")])
def test_an_unusable_price_is_refused_rather_than_quoted_as_free(amount: Decimal | None) -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        require_positive_price(amount, "USD")

    assert "price" in str(excinfo.value).lower()


def test_a_zero_price_the_platform_reported_is_passed_through() -> None:
    """A genuine 0.00 is the platform's own value; hiding it would be inventing one."""
    assert require_positive_price(Decimal("0.00"), "USD") == Decimal("0.00")


def test_a_price_without_a_currency_is_refused() -> None:
    with pytest.raises(InvalidInputError):
        require_positive_price(Decimal("8.06"), "")


# ------------------------------------------------------------------------ payment methods


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("Wallet", PaymentMethod.WALLET),
        ("wallet", PaymentMethod.WALLET),
        ("  WALLET  ", PaymentMethod.WALLET),
        ("Card", PaymentMethod.CARD),
        ("card", PaymentMethod.CARD),
    ],
)
def test_supported_payment_methods_are_accepted(supplied: str, expected: PaymentMethod) -> None:
    assert parse_payment_method(supplied) is expected


@pytest.mark.parametrize("supplied", ["DCB", "dcb", "PayPal", "crypto", "bank transfer", "!"])
def test_unsupported_payment_methods_are_refused(supplied: str) -> None:
    """DCB is a real backend payment type, deliberately not offered in this phase."""
    with pytest.raises(UnsupportedPaymentMethodError):
        parse_payment_method(supplied)


@pytest.mark.parametrize("supplied", [None, "", "   "])
def test_a_missing_payment_method_tells_the_model_to_ask_the_user(supplied: str | None) -> None:
    with pytest.raises(UnsupportedPaymentMethodError) as excinfo:
        parse_payment_method(supplied)

    message = str(excinfo.value).lower()
    assert "ask the user" in message
    assert "never choose for them" in message


# ------------------------------------------------------------------------ quote lifecycle


async def test_a_fresh_quote_is_active_and_carries_a_schema_version(quote_service: PurchaseQuoteService) -> None:
    quote = await create(quote_service)

    assert quote.status is QuoteStatus.ACTIVE
    assert quote.is_usable() is True
    assert quote.schema_version == QUOTE_SCHEMA_VERSION
    assert quote.expires_at > quote.created_at


async def test_a_quote_expires_after_the_configured_ttl(settings: Settings) -> None:
    short = settings.model_copy(update={"purchase_quote_ttl_seconds": 30})
    service = PurchaseQuoteService(short, InMemoryPurchaseQuoteStore())
    quote = await create(service)

    assert quote.seconds_remaining() <= 30
    later = utc_now() + timedelta(seconds=31)
    assert quote.is_expired(later) is True
    assert quote.effective_status(later) is QuoteStatus.EXPIRED


async def test_an_expired_quote_is_never_returned_as_active(quote_service: PurchaseQuoteService) -> None:
    quote = await create(quote_service)
    later = quote.expires_at + timedelta(seconds=1)

    with pytest.raises(QuoteExpiredError):
        await quote_service.get(OWNER_A, quote.quote_id, now=later)

    assert await quote_service.list_active(OWNER_A, now=later) == []


async def test_an_expired_quote_can_still_be_described_honestly(quote_service: PurchaseQuoteService) -> None:
    """A user asking "what did you prepare?" after expiry deserves an answer, not a 404."""
    quote = await create(quote_service)
    later = quote.expires_at + timedelta(seconds=1)

    record = await quote_service.get_record(OWNER_A, quote.quote_id)

    assert record.quote_id == quote.quote_id
    assert record.effective_status(later) is QuoteStatus.EXPIRED


async def test_expiry_is_decided_by_the_clock_not_by_a_stored_flag(quote_service: PurchaseQuoteService) -> None:
    """No sweeper runs, so a lapsed quote must not look active just because nothing swept it."""
    quote = await create(quote_service)
    later = quote.expires_at + timedelta(seconds=1)

    stored = await quote_service.get_record(OWNER_A, quote.quote_id)

    assert stored.status is QuoteStatus.ACTIVE  # never rewritten in storage
    assert stored.effective_status(later) is QuoteStatus.EXPIRED  # but reported as expired
    with pytest.raises(QuoteExpiredError):
        require_usable_quote(stored, now=later)


async def test_cancelling_a_quote_makes_it_unusable(quote_service: PurchaseQuoteService) -> None:
    quote = await create(quote_service)

    cancelled = await quote_service.cancel(OWNER_A, quote.quote_id)

    assert cancelled.status is QuoteStatus.CANCELLED
    with pytest.raises(QuoteCancelledError):
        await quote_service.get(OWNER_A, quote.quote_id)
    assert await quote_service.list_active(OWNER_A) == []


async def test_cancelling_twice_is_harmless(quote_service: PurchaseQuoteService) -> None:
    """There is no backend order to double-cancel, so the second call is a no-op."""
    quote = await create(quote_service)

    first = await quote_service.cancel(OWNER_A, quote.quote_id)
    second = await quote_service.cancel(OWNER_A, quote.quote_id)

    assert first.status is second.status is QuoteStatus.CANCELLED


async def test_cancelling_an_unknown_quote_is_refused(quote_service: PurchaseQuoteService) -> None:
    with pytest.raises(QuoteNotFoundError):
        await quote_service.cancel(OWNER_A, "no-such-quote")


def test_a_consumed_quote_cannot_be_reused() -> None:
    """Nothing in this phase sets CONSUMED; the gate exists for the execution phase."""
    quote = PurchaseQuote(
        quote_id=new_quote_id(),
        owner_session_key=OWNER_A.session_key,
        owner_user_ref=OWNER_A.user_ref,
        identity_source="test",
        bundle=BUNDLE,
        payment_method=PaymentMethod.CARD,
        price=PRICE,
        locale="en",
        expires_at=utc_now() + timedelta(minutes=5),
        status=QuoteStatus.CONSUMED,
    )

    with pytest.raises(QuoteConsumedError):
        require_usable_quote(quote)


# ---------------------------------------------------------------------------- ownership


async def test_one_user_cannot_read_another_users_quote(quote_service: PurchaseQuoteService) -> None:
    quote = await create(quote_service, OWNER_A)

    with pytest.raises(QuoteNotFoundError):
        await quote_service.get(OWNER_B, quote.quote_id)


async def test_one_user_cannot_cancel_another_users_quote(quote_service: PurchaseQuoteService) -> None:
    quote = await create(quote_service, OWNER_A)

    with pytest.raises(QuoteNotFoundError):
        await quote_service.cancel(OWNER_B, quote.quote_id)

    assert (await quote_service.get(OWNER_A, quote.quote_id)).status is QuoteStatus.ACTIVE


async def test_a_foreign_quote_is_invisible_rather_than_refused(quote_service: PurchaseQuoteService) -> None:
    """Identical answers for "does not exist" and "is not yours" -- no existence oracle."""
    quote = await create(quote_service, OWNER_A)

    with pytest.raises(QuoteNotFoundError) as foreign:
        await quote_service.get(OWNER_B, quote.quote_id)
    with pytest.raises(QuoteNotFoundError) as absent:
        await quote_service.get(OWNER_B, new_quote_id())

    assert str(foreign.value) == str(absent.value)


async def test_the_same_client_signed_in_as_a_different_user_gets_no_access(
    quote_service: PurchaseQuoteService,
) -> None:
    """Ownership is client *and* user: a re-login as somebody else must not inherit quotes."""
    quote = await create(quote_service, OWNER_A)

    with pytest.raises(QuoteNotFoundError):
        await quote_service.get(OWNER_A_OTHER_USER, quote.quote_id)


async def test_one_users_listing_never_includes_another_users_quotes(
    quote_service: PurchaseQuoteService,
) -> None:
    await create(quote_service, OWNER_A)
    await create(quote_service, OWNER_A, bundle=other_bundle("second"))
    await create(quote_service, OWNER_B, bundle=other_bundle("third"))

    assert len(await quote_service.list_active(OWNER_A)) == 2
    assert len(await quote_service.list_active(OWNER_B)) == 1


async def test_a_store_that_leaks_a_foreign_quote_is_caught_above_it(
    settings: Settings, quote_store: InMemoryPurchaseQuoteStore
) -> None:
    """Defence in depth for a future store implementation with an ownership bug."""

    class LeakyStore(InMemoryPurchaseQuoteStore):
        async def get(self, owner: QuoteOwner, quote_id: str) -> PurchaseQuote | None:
            # Deliberately ignores the owner, the way a mis-written Redis lookup might.
            return self._quotes.get(quote_id)

    leaky: PurchaseQuoteStore = LeakyStore()
    service = PurchaseQuoteService(settings, leaky)
    quote = await create(service, OWNER_A)

    with pytest.raises(QuoteNotOwnedError):
        await service.get(OWNER_B, quote.quote_id)


def test_owner_matching_uses_a_constant_time_comparison() -> None:
    quote = PurchaseQuote(
        quote_id=new_quote_id(),
        owner_session_key=OWNER_A.session_key,
        owner_user_ref=OWNER_A.user_ref,
        identity_source="test",
        bundle=BUNDLE,
        payment_method=PaymentMethod.CARD,
        price=PRICE,
        locale="en",
        expires_at=utc_now() + timedelta(minutes=5),
    )

    assert OWNER_A.matches(quote) is True
    assert OWNER_B.matches(quote) is False
    # Same session, different user, and vice versa: both halves must be checked.
    assert OWNER_A_OTHER_USER.matches(quote) is False
    assert QuoteOwner(session_key=OWNER_B.session_key, user_ref=OWNER_A.user_ref).matches(quote) is False


# -------------------------------------------------------------------- limits and duplicates


async def test_the_active_quote_limit_is_enforced(settings: Settings) -> None:
    capped = settings.model_copy(update={"max_active_quotes_per_user": 3})
    service = PurchaseQuoteService(capped, InMemoryPurchaseQuoteStore())
    for index in range(3):
        await create(service, bundle=other_bundle(f"bundle-{index}"))

    with pytest.raises(TooManyActiveQuotesError) as excinfo:
        await create(service, bundle=other_bundle("bundle-overflow"))

    assert "maximum" in str(excinfo.value).lower()


async def test_cancelling_frees_a_slot_under_the_limit(settings: Settings) -> None:
    capped = settings.model_copy(update={"max_active_quotes_per_user": 2})
    service = PurchaseQuoteService(capped, InMemoryPurchaseQuoteStore())
    first = await create(service, bundle=other_bundle("one"))
    await create(service, bundle=other_bundle("two"))

    await service.cancel(OWNER_A, first.quote_id)

    assert await create(service, bundle=other_bundle("three"))


async def test_the_limit_is_per_user_not_global(settings: Settings) -> None:
    capped = settings.model_copy(update={"max_active_quotes_per_user": 1})
    service = PurchaseQuoteService(capped, InMemoryPurchaseQuoteStore())
    await create(service, OWNER_A)

    assert await create(service, OWNER_B)


async def test_preparing_the_same_plan_again_supersedes_the_older_quote(
    quote_service: PurchaseQuoteService,
) -> None:
    """The documented duplicate policy: replace, so the numbers quoted are never stale."""
    first = await create(quote_service)

    second = await create(quote_service)

    assert second.quote_id != first.quote_id
    active = await quote_service.list_active(OWNER_A)
    assert [quote.quote_id for quote in active] == [second.quote_id]
    with pytest.raises(QuoteCancelledError):
        await quote_service.get(OWNER_A, first.quote_id)


async def test_re_preparing_the_same_plan_never_trips_the_limit(settings: Settings) -> None:
    """Superseding happens before the limit is counted, so a repeat call stays harmless."""
    capped = settings.model_copy(update={"max_active_quotes_per_user": 1})
    service = PurchaseQuoteService(capped, InMemoryPurchaseQuoteStore())
    await create(service)

    for _ in range(5):
        await create(service)

    assert len(await service.list_active(OWNER_A)) == 1


async def test_the_same_plan_with_a_different_payment_method_is_a_separate_quote(
    quote_service: PurchaseQuoteService,
) -> None:
    card = await create(quote_service, payment_method=PaymentMethod.CARD)

    wallet = await create(quote_service, payment_method=PaymentMethod.WALLET)

    active = {quote.quote_id for quote in await quote_service.list_active(OWNER_A)}
    assert active == {card.quote_id, wallet.quote_id}


async def test_concurrent_preparations_of_the_same_plan_leave_exactly_one_active(
    quote_service: PurchaseQuoteService,
) -> None:
    """The per-owner lock is what stops two racing calls both ending up active."""
    results = await asyncio.gather(*(create(quote_service) for _ in range(8)))

    active = await quote_service.list_active(OWNER_A)
    assert len(active) == 1
    assert active[0].quote_id in {quote.quote_id for quote in results}


async def test_concurrent_preparations_of_different_plans_all_survive(settings: Settings) -> None:
    roomy = settings.model_copy(update={"max_active_quotes_per_user": 10})
    service = PurchaseQuoteService(roomy, InMemoryPurchaseQuoteStore())

    await asyncio.gather(*(create(service, bundle=other_bundle(f"b-{index}")) for index in range(6)))

    assert len(await service.list_active(OWNER_A)) == 6


# ------------------------------------------------------------- session invalidation & store


async def test_invalidating_a_session_cancels_that_sessions_quotes(
    quote_service: PurchaseQuoteService,
) -> None:
    mine = await create(quote_service, OWNER_A)
    theirs = await create(quote_service, OWNER_B)

    cancelled = await quote_service.invalidate_session(OWNER_A.session_key)

    assert cancelled == 1
    with pytest.raises(QuoteCancelledError):
        await quote_service.get(OWNER_A, mine.quote_id)
    assert (await quote_service.get(OWNER_B, theirs.quote_id)).quote_id == theirs.quote_id


async def test_invalidating_a_session_covers_every_user_seen_on_that_client(
    quote_service: PurchaseQuoteService,
) -> None:
    """Logout is per MCP client, so every quote under that session key has to go."""
    first = await create(quote_service, OWNER_A)
    second = await create(quote_service, OWNER_A_OTHER_USER)

    assert await quote_service.invalidate_session(OWNER_A.session_key) == 2

    for owner, quote in ((OWNER_A, first), (OWNER_A_OTHER_USER, second)):
        with pytest.raises(QuoteCancelledError):
            await quote_service.get(owner, quote.quote_id)


async def test_invalidating_an_unknown_session_cancels_nothing(quote_service: PurchaseQuoteService) -> None:
    await create(quote_service, OWNER_A)

    assert await quote_service.invalidate_session("z" * 64) == 0


async def test_a_stored_quote_cannot_be_mutated_through_a_returned_copy(
    quote_service: PurchaseQuoteService, quote_store: InMemoryPurchaseQuoteStore
) -> None:
    quote = await create(quote_service)

    quote.bundle.name = "Tampered"
    quote.price.displayed_amount = Decimal("0.01")

    stored = await quote_store.get(OWNER_A, quote.quote_id)
    assert stored.bundle.name == "France 10GB"
    assert stored.price.displayed_amount == Decimal("8.06")


async def test_closing_the_store_drops_every_quote(quote_store: InMemoryPurchaseQuoteStore) -> None:
    """In-memory quotes do not survive a restart. Documented, and asserted."""
    service = PurchaseQuoteService(
        Settings.build(api_base_url="https://backend.test", device_id_salt="s" * 40), quote_store
    )
    quote = await create(service)

    await quote_store.aclose()

    assert await quote_store.get(OWNER_A, quote.quote_id) is None


# ------------------------------------------------------------------ execution-phase gate


def test_the_wallet_sufficiency_gate_blocks_an_underfunded_quote() -> None:
    """Not called in Phase 3; it is the check the execution phase must not skip."""
    quote = PurchaseQuote(
        quote_id=new_quote_id(),
        owner_session_key=OWNER_A.session_key,
        owner_user_ref=OWNER_A.user_ref,
        identity_source="test",
        bundle=BUNDLE,
        payment_method=PaymentMethod.WALLET,
        price=PRICE,
        wallet=evaluate_wallet(balance=Decimal("2.00"), currency="USD", price=Decimal("8.06")),
        locale="en",
        expires_at=utc_now() + timedelta(minutes=5),
    )

    with pytest.raises(InsufficientWalletBalanceError):
        require_wallet_sufficient(quote)


def test_the_wallet_sufficiency_gate_passes_a_funded_quote_and_ignores_card() -> None:
    funded = PurchaseQuote(
        quote_id=new_quote_id(),
        owner_session_key=OWNER_A.session_key,
        owner_user_ref=OWNER_A.user_ref,
        identity_source="test",
        bundle=BUNDLE,
        payment_method=PaymentMethod.WALLET,
        price=PRICE,
        wallet=evaluate_wallet(balance=Decimal("20.00"), currency="USD", price=Decimal("8.06")),
        locale="en",
        expires_at=utc_now() + timedelta(minutes=5),
    )
    card = funded.model_copy(update={"payment_method": PaymentMethod.CARD, "wallet": None})

    assert require_wallet_sufficient(funded) is funded
    assert require_wallet_sufficient(card) is card


# ------------------------------------------------------------------------ stored shape


def test_a_quote_stores_everything_the_execution_phase_will_need() -> None:
    fields = set(PurchaseQuote.model_fields)

    assert fields == {
        "quote_id",
        "schema_version",
        "owner_session_key",
        "owner_user_ref",
        "identity_source",
        "bundle",
        "payment_method",
        "price",
        "wallet",
        "search_context",
        "locale",
        "created_at",
        "expires_at",
        "status",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "access_token",
        "refresh_token",
        "token",
        "otp",
        "verification_pin",
        "client_secret",
        "stripe_client_secret",
        "card_number",
        "cvv",
        "pan",
        "supabase_key",
        "api_key",
        "password",
        "user_id",
        "email",
        "phone",
        "msisdn",
        "device_id",
        "order_id",
        "payment_intent",
    ],
)
def test_a_quote_can_never_hold_a_secret_or_a_raw_identifier(forbidden: str) -> None:
    """Asserted on the schema, so the rule cannot be broken by a later field addition."""
    for model in (PurchaseQuote, QuotedBundle, QuotedPrice, SearchContext):
        assert forbidden not in set(model.model_fields), f"{model.__name__} declares {forbidden!r}"


def test_a_quote_rejects_an_unexpected_field() -> None:
    with pytest.raises(ValueError):
        PurchaseQuote(
            quote_id=new_quote_id(),
            owner_session_key=OWNER_A.session_key,
            owner_user_ref=OWNER_A.user_ref,
            identity_source="test",
            bundle=BUNDLE,
            payment_method=PaymentMethod.CARD,
            price=PRICE,
            locale="en",
            expires_at=utc_now() + timedelta(minutes=5),
            access_token="a-token-that-must-not-be-storable",
        )


def test_a_price_is_never_marked_final_or_tax_confirmed() -> None:
    price = QuotedPrice(displayed_amount=Decimal("8.06"), currency="USD")

    assert price.final_amount_confirmed is False
    assert price.tax_included_confirmed is False


def test_search_context_defaults_to_incomplete() -> None:
    context = SearchContext()

    assert context.kind is SearchContextKind.NONE
    assert context.is_complete is False
