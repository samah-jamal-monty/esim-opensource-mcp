"""The purchase-preparation tools against a mocked backend.

Every test here mocks HTTP with ``respx``; nothing touches QA. The routes deliberately
mocked are only the three safe reads this phase is allowed to use, so a call to any other
route shows up as an unmocked-request error rather than passing silently.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from esim_mcp.errors import (
    AuthenticationRequiredError,
    BundleNotFoundError,
    BundleUnavailableError,
    InvalidInputError,
    QuoteCancelledError,
    QuoteNotFoundError,
    UnsupportedPaymentMethodError,
    WalletUnavailableError,
)
from esim_mcp.session.identity import ClientIdentityProvider
from esim_mcp.tools.authentication import AuthenticationService
from esim_mcp.tools.purchase_preparation import PurchasePreparationService
from tests.conftest import (
    API_URL,
    CATALOG_COUNTRIES,
    CATALOG_REGIONS,
    StubIdentityProvider,
    bundle_payload,
    envelope,
    mock_login_routes,
    sign_in,
    wallet_payload,
)

BUNDLE_CODE = "aaaaaaaa-0000-4000-8000-000000000001"
WALLET_PATH = f"{API_URL}/wallet/user_wallet_by_user"


def rendered(value: Any) -> str:
    return json.dumps(value, default=str)


@dataclass(slots=True)
class Backend:
    """The mocked backend, with handles to the routes tests need to count or re-stub.

    Route handles are captured at registration time on purpose: ``respx`` clears a route's
    mocked response when the same pattern is registered again, so re-deriving a handle with
    ``router.get(url)`` would silently un-stub it.
    """

    router: respx.Router
    bundle: respx.Route
    wallet: respx.Route

    @property
    def calls(self) -> Any:
        return self.router.calls

    def stub_bundle(self, payload: dict[str, Any]) -> None:
        self.bundle.mock(return_value=httpx.Response(200, json=envelope(payload)))

    def stub_wallet(self, payload: dict[str, Any] | None) -> None:
        self.wallet.mock(return_value=httpx.Response(200, json=envelope(payload)))


@pytest.fixture
def backend(respx_mock: respx.Router) -> Backend:
    """The three safe reads, plus the login routes. Nothing else is mocked, on purpose.

    Any call to a route outside this set fails the test as an unmocked request, which is how
    "no order, payment or provisioning route was touched" is enforced here.
    """
    mock_login_routes(respx_mock)
    bundle = respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload(bundle_code=BUNDLE_CODE, price=8.06)))
    )
    wallet = respx_mock.get(WALLET_PATH).mock(
        return_value=httpx.Response(200, json=envelope(wallet_payload(balance=20.0)))
    )
    respx_mock.get(f"{API_URL}/bundles/countries").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
    )
    respx_mock.get(f"{API_URL}/bundles/region").mock(return_value=httpx.Response(200, json=envelope(CATALOG_REGIONS)))
    respx_mock.post(f"{API_URL}/auth/logout").mock(return_value=httpx.Response(200, json=envelope(None)))
    return Backend(router=respx_mock, bundle=bundle, wallet=wallet)


@pytest.fixture
async def signed_in(service: AuthenticationService, backend: Backend) -> Backend:
    await sign_in(service)
    return backend


# ------------------------------------------------------------------------ authentication


async def test_preparation_requires_an_authenticated_user(
    purchase_service: PurchasePreparationService, backend: Backend
) -> None:
    """Browsing is login-free; preparing is not, because it reads the user's own wallet."""
    with pytest.raises(AuthenticationRequiredError):
        await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")


async def test_nothing_is_fetched_before_authentication_fails(
    purchase_service: PurchasePreparationService, backend: Backend
) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    assert not [call for call in backend.calls if "/bundles/" in call.request.url.path]


@pytest.mark.parametrize("tool", ["get_prepared_purchase", "cancel_prepared_purchase"])
async def test_reading_and_cancelling_also_require_authentication(
    purchase_service: PurchasePreparationService, backend: Backend, tool: str
) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await getattr(purchase_service, tool)(quote_id="anything")


# ------------------------------------------------------------------- authoritative reads


async def test_the_bundle_is_re_fetched_at_preparation_time(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """The catalogue result the model already has is never trusted as a source of price."""
    route = signed_in.bundle

    await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    assert route.call_count == 1


async def test_the_quoted_price_comes_from_the_backend_not_from_the_caller(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    signed_in.stub_bundle(bundle_payload(bundle_code=BUNDLE_CODE, price=99.99))

    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    assert result["pricing"]["displayed_amount"] == "99.99"


async def test_the_tool_has_no_argument_through_which_a_price_could_be_supplied(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """A hallucinated price cannot enter a quote because there is nowhere to put it."""
    with pytest.raises(TypeError):
        await purchase_service.prepare_purchase(
            bundle_code=BUNDLE_CODE,
            payment_method="Card",
            price="0.01",  # type: ignore[call-arg]
        )

    for forbidden in ("displayed_amount", "wallet_balance", "balance", "tax", "discount"):
        with pytest.raises(TypeError):
            await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card", **{forbidden: "1"})


async def test_an_inactive_bundle_is_refused(purchase_service: PurchasePreparationService, signed_in: Backend) -> None:
    signed_in.stub_bundle(bundle_payload(bundle_code=BUNDLE_CODE, is_active=False))

    with pytest.raises(BundleUnavailableError) as excinfo:
        await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    assert "no longer available" in str(excinfo.value).lower()


async def test_an_inactive_bundle_is_refused_before_the_wallet_is_read(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    signed_in.stub_bundle(bundle_payload(bundle_code=BUNDLE_CODE, is_active=False))
    wallet_route = signed_in.wallet

    with pytest.raises(BundleUnavailableError):
        await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")

    assert wallet_route.call_count == 0


async def test_an_unknown_bundle_is_refused(purchase_service: PurchasePreparationService, signed_in: Backend) -> None:
    signed_in.router.get(f"{API_URL}/bundles/no-such-bundle").mock(
        return_value=httpx.Response(404, json=envelope(None, status="failed", response_code=404))
    )

    with pytest.raises(BundleNotFoundError):
        await purchase_service.prepare_purchase(bundle_code="no-such-bundle", payment_method="Card")


async def test_a_bundle_without_a_price_is_never_quoted_as_free(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    payload = bundle_payload(bundle_code=BUNDLE_CODE)
    payload["price"] = None
    payload["price_display"] = None
    signed_in.stub_bundle(payload)

    with pytest.raises(InvalidInputError) as excinfo:
        await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    assert "price" in str(excinfo.value).lower()


# -------------------------------------------------------------------------- wallet quotes


async def test_a_wallet_quote_reads_the_authenticated_per_user_wallet_route(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    route = signed_in.wallet

    await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")

    assert route.call_count == 1
    assert route.calls.last.request.headers["Authorization"].startswith("Bearer ")


async def test_the_unauthenticated_by_id_wallet_route_is_never_called(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """``/wallet/user_wallet_by_id/{id}`` would expose any user's balance. It is banned."""
    await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")

    for call in signed_in.calls:
        assert "user_wallet_by_id" not in str(call.request.url)


async def test_a_sufficient_wallet_reports_the_estimated_remaining_balance(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")

    assert result["wallet"]["balance"] == "20.00"
    assert result["wallet"]["sufficient"] is True
    assert result["wallet"]["estimated_remaining_balance"] == "11.94"
    assert "shortfall" not in result["wallet"]


async def test_an_insufficient_wallet_still_produces_a_usable_quote(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """Not an error: the assistant needs the numbers to explain the shortfall."""
    signed_in.stub_wallet(wallet_payload(balance=2.0))

    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")

    assert result["status"] == "prepared"
    assert result["wallet"]["sufficient"] is False
    assert result["wallet"]["shortfall"] == "6.06"
    assert result["can_proceed_with_wallet"] is False
    assert "card" in result["precondition"].lower()
    assert result["order_created"] is False


async def test_wallet_money_survives_the_float_the_backend_sends(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """The backend declares ``balance: float``; the quote must still be exact to the cent."""
    signed_in.stub_bundle(bundle_payload(bundle_code=BUNDLE_CODE, price=0.1))
    signed_in.stub_wallet(wallet_payload(balance=0.3))

    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")

    assert result["pricing"]["displayed_amount"] == "0.10"
    assert result["wallet"]["balance"] == "0.30"
    assert result["wallet"]["estimated_remaining_balance"] == "0.20"


async def test_a_missing_wallet_is_reported_as_unavailable_not_as_zero(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """The backend answers a *successful* envelope with ``data: null`` for no wallet."""
    signed_in.stub_wallet(None)

    with pytest.raises(WalletUnavailableError) as excinfo:
        await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")

    assert "card" in str(excinfo.value).lower()


async def test_a_card_quote_never_reads_the_wallet(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    route = signed_in.wallet

    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    assert route.call_count == 0
    assert "wallet" not in result


# ---------------------------------------------------------------------------- card quotes


async def test_a_card_quote_creates_no_payment_intent_and_invents_no_link(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """Preparing a Card quote is still only a quote. The payment page belongs to another tool.

    That separation is the point: the link the user is sent to may only come from a tool the
    model reaches *after* the user has agreed to the amount, never from preparation itself.
    """
    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    assert result["card"]["payment_intent_created"] is False
    assert result["card"]["payment_link"] is None
    note = result["card_note"].lower()
    assert "no payment page exists yet" in note
    assert "never ask the user for card details" in note
    assert "never invent a payment link" in note
    assert "create_card_checkout" in note


async def test_a_card_quote_says_the_final_amount_is_not_confirmed(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    assert result["pricing"]["final_amount_confirmed"] is False
    assert result["pricing"]["tax_included_confirmed"] is False
    assert "final tax" in result["price_note"].lower()


@pytest.mark.parametrize("method", ["DCB", "dcb", "PayPal", ""])
async def test_an_unsupported_payment_method_is_refused(
    purchase_service: PurchasePreparationService, signed_in: Backend, method: str
) -> None:
    with pytest.raises(UnsupportedPaymentMethodError):
        await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method=method)


async def test_an_unsupported_payment_method_is_refused_before_any_backend_read(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    before = len(signed_in.calls)

    with pytest.raises(UnsupportedPaymentMethodError):
        await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="DCB")

    assert len(signed_in.calls) == before


# ------------------------------------------------------------------------- result contract


async def test_every_prepared_result_states_that_nothing_happened(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    for method in ("Wallet", "Card"):
        result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method=method)

        assert result["order_created"] is False
        assert result["charged"] is False
        assert result["message"] == "No order was created and nothing was charged."


async def test_the_result_carries_the_authoritative_plan_facts(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")

    assert result["bundle"]["bundle_code"] == BUNDLE_CODE
    assert result["bundle"]["name"] == "France 5GB / 30 Days"
    assert result["bundle"]["data"] == "5.0 GB"
    assert result["bundle"]["validity"] == "30 Day"
    assert result["payment_method"] == "Wallet"
    assert result["quote_id"]
    assert result["expires_at"]


async def test_a_card_result_routes_to_the_checkout_tool_without_promising_anything(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """The next step for a Card quote is a *conditional* one: only if the user agrees."""
    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    next_step = result["next_step"].lower()
    assert "no payment page exists yet and nothing was charged" in next_step
    assert "do not say the plan is reserved or held" in next_step
    assert "do not ask for card details" in next_step
    assert "if the user explicitly agrees" in next_step
    assert "create_card_checkout" in next_step


async def test_a_wallet_result_still_says_what_it_always_said(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """The Wallet branch is untouched by the card work, word for word."""
    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")

    next_step = result["next_step"].lower()
    assert "not available in this version" in next_step
    assert "do not say the plan is reserved or held" in next_step
    assert "do not ask for card details" in next_step
    assert "create_card_checkout" not in next_step


async def test_no_result_ever_contains_a_token_or_a_full_identifier(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    prepared = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")
    read = await purchase_service.get_prepared_purchase(quote_id=prepared["quote_id"])
    cancelled = await purchase_service.cancel_prepared_purchase(quote_id=prepared["quote_id"])

    for result in (prepared, read, cancelled):
        blob = rendered(result)
        assert "eyJ" not in blob, "a JWT reached a tool result"
        assert "refresh-token-value" not in blob
        assert "access_token" not in blob
        assert "refresh_token" not in blob
        assert "person@example.com" not in blob
        assert "b3f1c0de-1111-2222-3333-444455556666" not in blob
        lowered = blob.lower()
        for forbidden in ("device_id", "session_key", "user_id", "msisdn"):
            assert forbidden not in lowered


# ------------------------------------------------------------------------ search context


async def test_a_country_context_is_resolved_against_the_platforms_own_list(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """Recorded from the authoritative country record, not from the words the model passed."""
    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card", country="FR")

    assert result["search_context"]["kind"] == "country"
    assert result["search_context"]["country"] == "France"
    assert result["search_context"]["is_complete"] is True


async def test_a_region_context_is_resolved_against_the_platforms_own_list(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card", region="Europe")

    assert result["search_context"]["kind"] == "region"
    assert result["search_context"]["region"] == "Europe"
    assert result["search_context"]["is_complete"] is True


async def test_an_unresolvable_context_marks_the_quote_incomplete_instead_of_failing(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """Losing context costs a later lookup; failing would cost the user their choice."""
    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card", country="Atlantis")

    assert result["status"] == "prepared"
    assert result["search_context"]["is_complete"] is False
    assert "could not be confirmed" in result["search_context_note"]


async def test_no_context_at_all_is_allowed_and_marked_incomplete(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    assert result["search_context"] == {"kind": "none", "is_complete": False}


async def test_supplying_both_a_country_and_a_region_is_refused(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        await purchase_service.prepare_purchase(
            bundle_code=BUNDLE_CODE, payment_method="Card", country="France", region="Europe"
        )

    assert "not both" in str(excinfo.value).lower()


# ------------------------------------------------------------------------- read and cancel


async def test_a_prepared_quote_can_be_read_back(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    prepared = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")

    read = await purchase_service.get_prepared_purchase(quote_id=prepared["quote_id"])

    assert read["quote_id"] == prepared["quote_id"]
    assert read["pricing"] == prepared["pricing"]
    assert read["wallet"]["balance"] == "20.00"
    assert read["order_created"] is False


async def test_reading_a_quote_never_calls_the_backend(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    prepared = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")
    before = len(signed_in.calls)

    await purchase_service.get_prepared_purchase(quote_id=prepared["quote_id"])

    assert len(signed_in.calls) == before


async def test_cancelling_a_quote_never_calls_the_backend_and_claims_no_order(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    prepared = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")
    before = len(signed_in.calls)

    result = await purchase_service.cancel_prepared_purchase(quote_id=prepared["quote_id"])

    assert len(signed_in.calls) == before
    assert result["status"] == "cancelled"
    assert result["order_cancelled"] is False
    assert result["message"] == "The local quote was cancelled. No backend order existed."


async def test_a_cancelled_quote_reads_back_as_cancelled(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    prepared = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")
    await purchase_service.cancel_prepared_purchase(quote_id=prepared["quote_id"])

    read = await purchase_service.get_prepared_purchase(quote_id=prepared["quote_id"])

    assert read["status"] == "cancelled"
    assert read["order_created"] is False
    assert read["charged"] is False
    assert "prepare it again" in read["note"].lower()


async def test_an_expired_quote_reads_back_as_expired(
    purchase_service: PurchasePreparationService,
    signed_in: Backend,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    # Move the stored quote's expiry into the past rather than sleeping.
    store = purchase_service._quotes._store  # white-box: reaching into the store beats sleeping for a TTL
    stored = store._quotes[prepared["quote_id"]]
    store._quotes[prepared["quote_id"]] = stored.model_copy(update={"expires_at": stored.created_at})

    read = await purchase_service.get_prepared_purchase(quote_id=prepared["quote_id"])

    assert read["status"] == "expired"
    assert read["order_created"] is False
    assert "prepare the same plan again" in read["note"].lower()


@pytest.mark.parametrize("quote_id", ["", "   "])
async def test_an_empty_quote_reference_is_refused(
    purchase_service: PurchasePreparationService, signed_in: Backend, quote_id: str
) -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        await purchase_service.get_prepared_purchase(quote_id=quote_id)

    assert "never invent one" in str(excinfo.value).lower()


async def test_an_unknown_quote_reference_is_refused(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    with pytest.raises(QuoteNotFoundError):
        await purchase_service.get_prepared_purchase(quote_id="not-a-real-quote-id")


async def test_preparing_the_same_choice_twice_replaces_the_first_quote(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    first = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    second = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    assert second["quote_id"] != first["quote_id"]
    stale = await purchase_service.get_prepared_purchase(quote_id=first["quote_id"])
    assert stale["status"] == "cancelled"


# ---------------------------------------------------------------- isolation between clients


async def test_one_client_cannot_read_another_clients_quote(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    make_purchase_service: Callable[[ClientIdentityProvider], PurchasePreparationService],
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    backend: Backend,
) -> None:
    await sign_in(make_service(identity_a))
    await sign_in(make_service(identity_b), email="other@example.com")
    purchase_a = make_purchase_service(identity_a)
    purchase_b = make_purchase_service(identity_b)
    prepared = await purchase_a.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    with pytest.raises(QuoteNotFoundError):
        await purchase_b.get_prepared_purchase(quote_id=prepared["quote_id"])


async def test_one_client_cannot_cancel_another_clients_quote(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    make_purchase_service: Callable[[ClientIdentityProvider], PurchasePreparationService],
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    backend: Backend,
) -> None:
    await sign_in(make_service(identity_a))
    await sign_in(make_service(identity_b), email="other@example.com")
    purchase_a = make_purchase_service(identity_a)
    purchase_b = make_purchase_service(identity_b)
    prepared = await purchase_a.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    with pytest.raises(QuoteNotFoundError):
        await purchase_b.cancel_prepared_purchase(quote_id=prepared["quote_id"])

    assert (await purchase_a.get_prepared_purchase(quote_id=prepared["quote_id"]))["status"] == "prepared"


# ------------------------------------------------------------------------------- logout


async def test_logout_invalidates_every_quote_of_that_session(
    service: AuthenticationService,
    purchase_service: PurchasePreparationService,
    signed_in: Backend,
) -> None:
    prepared = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    await service.logout()
    await sign_in(service)

    with pytest.raises(QuoteCancelledError):
        await purchase_service._quotes.get(  # the tool layer would report it as cancelled
            await _owner_of(purchase_service), prepared["quote_id"]
        )


async def test_a_quote_cannot_be_read_after_logout_even_by_the_same_client(
    service: AuthenticationService,
    purchase_service: PurchasePreparationService,
    signed_in: Backend,
) -> None:
    """The security case: whoever signs in next on this client must not inherit the quote."""
    prepared = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    await service.logout()
    await sign_in(service)

    read = await purchase_service.get_prepared_purchase(quote_id=prepared["quote_id"])
    assert read["status"] == "cancelled"


async def test_logout_leaves_another_clients_quotes_alone(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    make_purchase_service: Callable[[ClientIdentityProvider], PurchasePreparationService],
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    backend: Backend,
) -> None:
    auth_a, auth_b = make_service(identity_a), make_service(identity_b)
    await sign_in(auth_a)
    await sign_in(auth_b, email="other@example.com")
    purchase_b = make_purchase_service(identity_b)
    theirs = await purchase_b.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Card")

    await auth_a.logout()

    assert (await purchase_b.get_prepared_purchase(quote_id=theirs["quote_id"]))["status"] == "prepared"


async def _owner_of(purchase_service: PurchasePreparationService) -> Any:
    _, _, _, owner = await purchase_service._owner(None)
    return owner


# -------------------------------------------------------------------------- money typing


async def test_no_result_serializes_a_monetary_value_as_a_float(
    purchase_service: PurchasePreparationService, signed_in: Backend
) -> None:
    """Strings on the wire: a float amount could be re-read with a rounding artifact."""
    result = await purchase_service.prepare_purchase(bundle_code=BUNDLE_CODE, payment_method="Wallet")

    assert isinstance(result["pricing"]["displayed_amount"], str)
    assert isinstance(result["wallet"]["balance"], str)
    assert isinstance(result["wallet"]["estimated_remaining_balance"], str)
    assert Decimal(result["wallet"]["estimated_remaining_balance"]) == Decimal("11.94")
