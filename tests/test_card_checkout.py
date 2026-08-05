"""``create_card_checkout`` and ``check_card_payment_status`` against a mocked backend.

No test here reaches QA, none opens a real payment page and none pays for anything.
Everything is stubbed with ``respx``: only the two MCP card routes are mocked, and a call to
any *other* route surfaces as an unmocked-request error rather than passing silently. That is
what keeps "this suite cannot pay for anything" a property of the harness rather than a
promise in a comment.

The properties asserted here are the ones that cost real money, or real trust, if they
regress:

* a checkout needs a session, an owned quote, a usable quote and a **Card** quote;
* one idempotency key is minted per quote and **reused**, so a second page cannot exist;
* a repeated call replays the first link instead of opening another;
* a payment can only be read by the client that started it;
* a redirect, a returning user and a success screen are never treated as payment;
* every payment state maps to the right thing to tell the user;
* no token, key, provider session id or client secret ever reaches a result or a log.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx

from esim_mcp.errors import (
    AuthenticationRequiredError,
    CardCheckoutAttemptLimitError,
    CardCheckoutOutcomeUnknownError,
    CardCheckoutRejectedError,
    CardCheckoutUnavailableError,
    CardPaymentAmbiguousError,
    CardPaymentCheckLimitError,
    CardPaymentNotFoundError,
    CardPaymentStatusUnavailableError,
    ForbiddenBackendRouteError,
    IdempotencyConflictError,
    InvalidInputError,
    NonCardQuoteError,
    NonWalletQuoteError,
    QuoteCancelledError,
    QuoteConsumedError,
    QuoteExpiredError,
    QuoteNotFoundError,
    RateLimitedError,
    UnsafeCheckoutLinkError,
)
from esim_mcp.models.card import CardPaymentStatus, safe_checkout_url
from esim_mcp.purchase.card import MAX_CHECKOUT_ATTEMPTS, MAX_STATUS_CHECKS, InMemoryCardCheckoutStore
from esim_mcp.purchase.models import QuoteStatus
from esim_mcp.purchase.service import PurchaseQuoteService
from esim_mcp.purchase.store import QuoteOwner
from esim_mcp.session.identity import ClientIdentityProvider
from esim_mcp.settings import Settings
from esim_mcp.tools.authentication import AuthenticationService
from esim_mcp.tools.card_checkout import CardPaymentService
from esim_mcp.tools.purchase_execution import PurchaseConfirmationService
from esim_mcp.tools.purchase_preparation import PurchasePreparationService
from tests.conftest import (
    API_URL,
    CATALOG_COUNTRIES,
    CHECKOUT_URL,
    ORDER_ID,
    PAYMENT_REFERENCE,
    StubIdentityProvider,
    bundle_payload,
    card_checkout_payload,
    card_status_payload,
    envelope,
    mock_login_routes,
    sign_in,
    wallet_payload,
)

BUNDLE_CODE = "aaaaaaaa-0000-4000-8000-000000000001"
CHECKOUT_PATH = "/mcp/user/bundle/card/checkout"
CHECKOUT_URL_ENDPOINT = f"{API_URL}{CHECKOUT_PATH}"
STATUS_URL = f"{API_URL}/mcp/user/bundle/card/status/{PAYMENT_REFERENCE}"

#: The backend's accepted idempotency-key alphabet and length window.
KEY_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")
KEY_MIN_LENGTH = 32
KEY_MAX_LENGTH = 128

#: Values a fixture payload carries that must never reach a result, a log or an error.
PROVIDER_SECRETS = ("sess_secret_value_0001", "secret_do_not_leak_0001", "c0rr3lat10n")


def rendered(value: Any) -> str:
    return json.dumps(value, default=str)


@dataclass(slots=True)
class Backend:
    """The mocked backend, with handles to the two card routes."""

    router: respx.Router
    checkout: respx.Route
    status: respx.Route
    bundle: respx.Route

    @property
    def checkout_calls(self) -> list[httpx.Request]:
        return [call.request for call in self.router.calls if call.request.url.path.endswith(CHECKOUT_PATH)]

    @property
    def status_calls(self) -> list[httpx.Request]:
        return [call.request for call in self.router.calls if "/card/status/" in call.request.url.path]

    @property
    def sent_keys(self) -> list[str]:
        return [request.headers["Idempotency-Key"] for request in self.checkout_calls]

    def stub_checkout(self, response: httpx.Response) -> None:
        self.checkout.mock(return_value=response)

    def stub_checkout_error(self, error: Exception) -> None:
        self.checkout.mock(side_effect=error)

    def stub_status(self, response: httpx.Response) -> None:
        self.status.mock(return_value=response)

    def stub_status_error(self, error: Exception) -> None:
        self.status.mock(side_effect=error)


def failure_response(status: int, *, title: str, data: dict[str, Any] | None = None) -> httpx.Response:
    """A backend failure envelope, shaped the way the platform actually sends one."""
    return httpx.Response(
        status,
        json=envelope(
            data,
            status="failed",
            title=title,
            message="localized text the client never sees",
            developer_message=title,
            response_code=status,
        ),
    )


@pytest.fixture
def backend(respx_mock: respx.Router) -> Backend:
    mock_login_routes(respx_mock)
    checkout = respx_mock.post(CHECKOUT_URL_ENDPOINT).mock(
        return_value=httpx.Response(200, json=envelope(card_checkout_payload()))
    )
    status = respx_mock.get(STATUS_URL).mock(return_value=httpx.Response(200, json=envelope(card_status_payload())))
    bundle = respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload(bundle_code=BUNDLE_CODE, price=8.06)))
    )
    respx_mock.get(f"{API_URL}/wallet/user_wallet_by_user").mock(
        return_value=httpx.Response(200, json=envelope(wallet_payload(balance=20.0)))
    )
    respx_mock.get(f"{API_URL}/bundles/countries").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
    )
    respx_mock.post(f"{API_URL}/auth/logout").mock(return_value=httpx.Response(200, json=envelope(None)))
    return Backend(router=respx_mock, checkout=checkout, status=status, bundle=bundle)


@pytest.fixture
async def signed_in(service: AuthenticationService, backend: Backend) -> Backend:
    await sign_in(service)
    return backend


async def prepare(
    purchase_service: PurchasePreparationService,
    *,
    payment_method: str = "Card",
    country: str | None = None,
) -> str:
    """Prepare a quote through the real preparation tool and return its reference."""
    result = await purchase_service.prepare_purchase(
        bundle_code=BUNDLE_CODE, payment_method=payment_method, country=country
    )
    return result["quote_id"]


async def open_checkout(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
) -> dict[str, Any]:
    """Prepare a Card quote and open its payment page."""
    reference = await prepare(purchase_service)
    return await card_service.create_card_checkout(quote_reference=reference)


# ------------------------------------------------------------------------ authentication


async def test_opening_a_checkout_requires_an_authenticated_user(
    card_service: CardPaymentService, backend: Backend
) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await card_service.create_card_checkout(quote_reference="anything")

    assert not backend.checkout_calls, "an unauthenticated call reached the checkout route"


async def test_checking_a_payment_requires_an_authenticated_user(
    card_service: CardPaymentService, backend: Backend
) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert not backend.status_calls, "an unauthenticated call reached the status route"


async def test_a_signed_out_user_cannot_open_a_checkout_for_an_earlier_quote(
    service: AuthenticationService,
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
) -> None:
    """A quote -- and its idempotency key -- must not outlive the session that made it."""
    reference = await prepare(purchase_service)
    await service.logout()

    with pytest.raises(AuthenticationRequiredError):
        await card_service.create_card_checkout(quote_reference=reference)

    assert not signed_in.checkout_calls


async def test_signing_out_drops_the_checkout_record_and_its_key(
    service: AuthenticationService,
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    checkout_store: InMemoryCardCheckoutStore,
    signed_in: Backend,
) -> None:
    result = await open_checkout(purchase_service, card_service)
    assert result["quote_reference"] in checkout_store._checkouts

    await service.logout()

    assert not checkout_store._checkouts, "a checkout key outlived the session that minted it"


async def test_a_payment_cannot_be_checked_after_signing_out(
    service: AuthenticationService,
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
) -> None:
    await open_checkout(purchase_service, card_service)
    await service.logout()

    with pytest.raises(AuthenticationRequiredError):
        await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert not signed_in.status_calls


# -------------------------------------------------------------------------------- quotes


async def test_an_unknown_quote_never_reaches_the_platform(
    card_service: CardPaymentService, signed_in: Backend
) -> None:
    with pytest.raises(QuoteNotFoundError):
        await card_service.create_card_checkout(quote_reference="not-a-quote-anyone-prepared")

    assert not signed_in.checkout_calls


@pytest.mark.parametrize("reference", ["", "   "])
async def test_a_missing_quote_reference_is_refused_with_guidance(
    card_service: CardPaymentService, signed_in: Backend, reference: str
) -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        await card_service.create_card_checkout(quote_reference=reference)

    assert "never invent one" in str(excinfo.value).lower()
    assert not signed_in.checkout_calls


async def test_another_clients_quote_is_invisible_rather_than_refused(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    make_purchase_service: Callable[[ClientIdentityProvider], PurchasePreparationService],
    make_card_service: Callable[[ClientIdentityProvider], CardPaymentService],
    identity_b: StubIdentityProvider,
    purchase_service: PurchasePreparationService,
    signed_in: Backend,
) -> None:
    """Client B must not be able to pay for -- or learn about -- client A's quote."""
    reference = await prepare(purchase_service)

    await sign_in(make_service(identity_b))
    with pytest.raises(QuoteNotFoundError):
        await make_card_service(identity_b).create_card_checkout(quote_reference=reference)

    assert not signed_in.checkout_calls


async def test_a_wallet_quote_cannot_be_paid_by_card(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """The refusal is local: routing a wallet quote through a card page must never be sent."""
    reference = await prepare(purchase_service, payment_method="Wallet")

    with pytest.raises(NonCardQuoteError) as excinfo:
        await card_service.create_card_checkout(quote_reference=reference)

    assert "wallet" in str(excinfo.value).lower()
    assert not signed_in.checkout_calls


async def test_an_expired_quote_is_refused_before_any_request(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    quote_service: PurchaseQuoteService,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service)
    stored = quote_service._store._quotes[reference]
    await quote_service._store.save(stored.model_copy(update={"expires_at": stored.created_at}))

    with pytest.raises(QuoteExpiredError):
        await card_service.create_card_checkout(quote_reference=reference)

    assert not signed_in.checkout_calls


async def test_a_cancelled_quote_is_refused_before_any_request(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    reference = await prepare(purchase_service)
    await purchase_service.cancel_prepared_purchase(quote_id=reference)

    with pytest.raises(QuoteCancelledError):
        await card_service.create_card_checkout(quote_reference=reference)

    assert not signed_in.checkout_calls


async def test_a_consumed_quote_is_refused_before_any_request(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    quote_service: PurchaseQuoteService,
    signed_in: Backend,
) -> None:
    """A quote spent on a completed payment can never open a second page."""
    reference = await prepare(purchase_service)
    await quote_service.consume(_quote_owner(quote_service, reference), reference)

    with pytest.raises(QuoteConsumedError):
        await card_service.create_card_checkout(quote_reference=reference)

    assert not signed_in.checkout_calls


def _quote_owner(quote_service: PurchaseQuoteService, quote_id: str) -> QuoteOwner:
    """The stored quote's own owner, so a test never re-derives an owner key by hand."""
    stored = quote_service._store._quotes[quote_id]
    return QuoteOwner(session_key=stored.owner_session_key, user_ref=stored.owner_user_ref)


# ------------------------------------------------------------------- opening a checkout


async def test_a_checkout_returns_the_link_the_amount_and_the_reference(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    result = await open_checkout(purchase_service, card_service)

    assert result["status"] == "checkout_ready"
    assert result["checkout_url"] == CHECKOUT_URL
    assert result["payment_reference"] == PAYMENT_REFERENCE
    assert result["amount"] == "8.06"
    assert result["currency"] == "USD"
    assert result["expires_at"] == "2026-01-01T00:30:00+00:00"
    assert result["payment_status"] == "PENDING"
    assert result["payment_method"] == "Card"


async def test_opening_a_checkout_never_claims_a_payment(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """A page is not a payment, and every branch has to say so."""
    result = await open_checkout(purchase_service, card_service)

    assert result["charged"] is False
    assert result["paid"] is False
    assert result["provisioned"] is False
    assert "nothing has been charged yet" in result["message"].lower()


async def test_the_pending_order_is_reported_as_unpaid_not_as_a_purchase(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """The backend records an order alongside the page. It is unpaid, and must read as unpaid."""
    result = await open_checkout(purchase_service, card_service)

    assert result["order_id"] == ORDER_ID
    assert result["order_state"] == "unpaid"
    assert result["paid"] is False
    next_step = result["next_step"].lower()
    assert "unpaid order" in next_step
    assert "never describe it as bought, placed or reserved" in next_step


async def test_the_exact_documented_checkout_envelope_parses(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """The backend's create-checkout response, field for field, with nothing added by us."""
    signed_in.stub_checkout(
        httpx.Response(
            200,
            json=envelope(
                {
                    "payment_reference": PAYMENT_REFERENCE,
                    "order_id": ORDER_ID,
                    "checkout_url": CHECKOUT_URL,
                    "status": "PENDING",
                    "amount": "10.00",
                    "currency": "USD",
                    "expires_at": "2026-01-01T00:30:00+00:00",
                    "idempotent_replay": False,
                    "correlation_id": "c0rr3lat10n",
                    "message": None,
                }
            ),
        )
    )

    result = await open_checkout(purchase_service, card_service)

    assert result["payment_reference"] == PAYMENT_REFERENCE
    assert result["order_id"] == ORDER_ID
    assert result["checkout_url"] == CHECKOUT_URL
    assert result["payment_status"] == "PENDING"
    assert result["amount"] == "10.00"
    assert result["currency"] == "USD"
    assert result["expires_at"] == "2026-01-01T00:30:00+00:00"
    assert result["replayed"] is False
    # The tracing handle is read and then dropped; it is not a fact for a conversation.
    assert "correlation_id" not in result
    assert "c0rr3lat10n" not in rendered(result)


async def test_an_idempotent_replay_flag_from_the_platform_is_reported(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    signed_in.stub_checkout(httpx.Response(200, json=envelope(card_checkout_payload(idempotent_replay=True))))

    result = await open_checkout(purchase_service, card_service)

    assert result["replayed"] is True


async def test_the_result_tells_the_model_never_to_ask_for_card_details(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    result = await open_checkout(purchase_service, card_service)

    assert "never" in result["card_entry_note"].lower()
    assert "card number" in result["card_entry_note"].lower()
    next_step = result["next_step"].lower()
    assert "never ask them for a card number" in next_step
    assert "security code" in next_step


async def test_the_result_says_a_redirect_is_not_proof_of_payment(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    result = await open_checkout(purchase_service, card_service)

    proof = result["proof_note"].lower()
    assert "not proof of payment" in proof
    assert "redirect" in proof


async def test_the_outbound_body_is_exactly_the_three_contract_fields(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """The endpoint declares ``extra="forbid"``: one extra key is a 422 for the whole request."""
    reference = await prepare(purchase_service, country="France")

    await card_service.create_card_checkout(quote_reference=reference)

    body = json.loads(signed_in.checkout_calls[0].content)
    assert body == {
        "bundle_code": BUNDLE_CODE,
        "quote_reference": reference,
        "related_search": {"countries": [{"iso3_code": "FRA", "country_name": "France"}]},
    }
    assert set(body) == {"bundle_code", "quote_reference", "related_search"}


async def test_the_outbound_body_never_carries_a_payment_type(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """The backend fixes the payment type itself; sending one is both redundant and a 422."""
    await open_checkout(purchase_service, card_service)

    body = json.loads(signed_in.checkout_calls[0].content)
    assert "payment_type" not in body
    assert "Card" not in json.dumps(body)


async def test_the_outbound_body_carries_no_money_identity_or_card_field(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """Nothing priced, nothing identifying and nothing secret may reach the request body."""
    await open_checkout(purchase_service, card_service)

    body = json.loads(signed_in.checkout_calls[0].content)
    rendered_body = json.dumps(body).lower()
    for forbidden in (
        "payment_type",
        "amount",
        "price",
        "total",
        "tax",
        "discount",
        "currency",
        "user_id",
        "user_token",
        "order_id",
        "checkout_url",
        "return_url",
        "success_url",
        "cancel_url",
        "card",
        "card_number",
        "cvv",
        "cvc",
        "expiry",
        "cardholder",
        "payment_method_id",
        "payment_token",
        "stripe",
        "token",
        "access_token",
        "idempotency_key",
        "device_id",
        "session",
    ):
        assert forbidden not in body, f"the request body carries {forbidden!r}"
        assert forbidden not in rendered_body, f"the request body mentions {forbidden!r}"


async def test_a_quote_with_no_search_context_omits_the_optional_field(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """Omitted rather than sent as null: an optional field is not a field to send empty."""
    await open_checkout(purchase_service, card_service)

    body = json.loads(signed_in.checkout_calls[0].content)
    assert set(body) == {"bundle_code", "quote_reference"}


async def test_the_currency_travels_only_as_a_header(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    await open_checkout(purchase_service, card_service)

    request = signed_in.checkout_calls[0]
    assert request.headers["X-Currency"] == "USD"
    assert "currency" not in json.loads(request.content)


async def test_the_documented_headers_are_forwarded(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    await open_checkout(purchase_service, card_service)

    headers = signed_in.checkout_calls[0].headers
    assert headers["Authorization"].startswith("Bearer ")
    assert headers["X-Device-Id"]
    assert headers["X-Currency"] == "USD"
    assert headers["Accept-Language"] == "en"
    assert headers["Idempotency-Key"]


async def test_the_platform_amount_wins_over_the_quoted_one(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """The platform applies the final tax, so its figure is the one the user is told."""
    signed_in.stub_checkout(httpx.Response(200, json=envelope(card_checkout_payload(amount=9.27))))

    result = await open_checkout(purchase_service, card_service)

    assert result["amount"] == "9.27"


async def test_a_missing_platform_amount_falls_back_to_the_quoted_one(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    signed_in.stub_checkout(httpx.Response(200, json=envelope(card_checkout_payload(amount=None))))

    result = await open_checkout(purchase_service, card_service)

    assert result["amount"] == "8.06"


# --------------------------------------------------------------- idempotency and replay


async def test_one_key_is_minted_per_quote_and_looks_the_way_the_platform_wants(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    await open_checkout(purchase_service, card_service)

    key = signed_in.sent_keys[0]
    assert KEY_MIN_LENGTH <= len(key) <= KEY_MAX_LENGTH
    assert set(key) <= KEY_ALPHABET


async def test_a_repeated_call_replays_the_same_link_and_sends_nothing(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """The property that stops a user being shown two pages for one plan."""
    reference = await prepare(purchase_service)

    first = await card_service.create_card_checkout(quote_reference=reference)
    second = await card_service.create_card_checkout(quote_reference=reference)

    assert len(signed_in.checkout_calls) == 1
    assert second["checkout_url"] == first["checkout_url"]
    assert second["payment_reference"] == first["payment_reference"]
    assert second["replayed"] is True
    assert "not a second one" in second["replay_note"].lower()


async def test_a_replayed_call_still_reports_nothing_charged(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    reference = await prepare(purchase_service)
    await card_service.create_card_checkout(quote_reference=reference)

    replayed = await card_service.create_card_checkout(quote_reference=reference)

    assert replayed["charged"] is False
    assert replayed["paid"] is False


async def test_a_retry_after_an_unclear_answer_reuses_the_same_key(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """A second key is the only way a second payment page could come into existence."""
    reference = await prepare(purchase_service)
    signed_in.stub_checkout_error(httpx.ReadTimeout("boom"))

    with pytest.raises(CardCheckoutOutcomeUnknownError):
        await card_service.create_card_checkout(quote_reference=reference)

    signed_in.stub_checkout(httpx.Response(200, json=envelope(card_checkout_payload(idempotent_replay=True))))
    result = await card_service.create_card_checkout(quote_reference=reference)

    assert len(signed_in.sent_keys) == 2
    assert signed_in.sent_keys[0] == signed_in.sent_keys[1]
    assert result["replayed"] is True


async def test_a_new_quote_gets_a_new_key(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    first = await prepare(purchase_service)
    await card_service.create_card_checkout(quote_reference=first)
    # Re-preparing the same plan supersedes the old quote and produces a fresh one.
    second = await prepare(purchase_service)
    await card_service.create_card_checkout(quote_reference=second)

    assert first != second
    assert signed_in.sent_keys[0] != signed_in.sent_keys[1]


async def test_two_concurrent_calls_for_one_quote_open_one_page(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    import asyncio

    reference = await prepare(purchase_service)

    results = await asyncio.gather(
        card_service.create_card_checkout(quote_reference=reference),
        card_service.create_card_checkout(quote_reference=reference),
    )

    assert len(signed_in.checkout_calls) == 1
    assert {result["checkout_url"] for result in results} == {CHECKOUT_URL}


async def test_attempts_are_bounded(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_checkout_error(httpx.ReadTimeout("boom"))

    for _ in range(MAX_CHECKOUT_ATTEMPTS):
        with pytest.raises(CardCheckoutOutcomeUnknownError):
            await card_service.create_card_checkout(quote_reference=reference)

    with pytest.raises(CardCheckoutAttemptLimitError):
        await card_service.create_card_checkout(quote_reference=reference)

    assert len(signed_in.checkout_calls) == MAX_CHECKOUT_ATTEMPTS


# ------------------------------------------------------------------- unhappy checkouts


async def test_a_timeout_is_reported_as_unknown_and_never_as_a_charge(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_checkout_error(httpx.ReadTimeout("boom"))

    with pytest.raises(CardCheckoutOutcomeUnknownError) as excinfo:
        await card_service.create_card_checkout(quote_reference=reference)

    message = str(excinfo.value).lower()
    assert "nothing has been charged" in message
    assert excinfo.value.details["charged"] is False


@pytest.mark.parametrize(
    ("status", "title", "expected"),
    [
        (400, "MCP_BUNDLE_NOT_AVAILABLE", "bundle_unavailable"),
        (400, "MCP_UNSUPPORTED_CURRENCY", "purchase_currency_mismatch"),
        (400, "MCP_UNSUPPORTED_PAYMENT_TYPE", "unsupported_quote_payment_method_for_card"),
        (422, "MCP_VALIDATION_FAILED", "card_checkout_rejected"),
        (409, "MCP_IDEMPOTENCY_CONFLICT", "purchase_idempotency_conflict"),
        (503, "MCP_CARD_DISABLED", "card_checkout_unavailable"),
        (404, "NOT_FOUND", "card_checkout_unavailable"),
        (429, "TOO_MANY_REQUESTS", "rate_limited"),
        (500, "INTERNAL", "card_checkout_outcome_unknown"),
    ],
)
async def test_backend_failures_map_to_typed_errors(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    status: int,
    title: str,
    expected: str,
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_checkout(failure_response(status, title=title))

    with pytest.raises(Exception) as excinfo:
        await card_service.create_card_checkout(quote_reference=reference)

    assert getattr(excinfo.value, "code", None) == expected


@pytest.mark.parametrize(
    ("status", "title", "error_type"),
    [
        (401, "UNAUTHORIZED", AuthenticationRequiredError),
        (429, "TOO_MANY_REQUESTS", RateLimitedError),
        (404, "NOT_FOUND", CardCheckoutUnavailableError),
    ],
)
async def test_a_refusal_before_anything_opened_can_be_tried_again(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    status: int,
    title: str,
    error_type: type[Exception],
) -> None:
    """Nothing was opened and nothing was charged, so the record stays retryable."""
    reference = await prepare(purchase_service)
    signed_in.stub_checkout(failure_response(status, title=title))

    with pytest.raises(error_type) as excinfo:
        await card_service.create_card_checkout(quote_reference=reference)
    assert "nothing was charged" in str(excinfo.value).lower()

    signed_in.stub_checkout(httpx.Response(200, json=envelope(card_checkout_payload())))
    result = await card_service.create_card_checkout(quote_reference=reference)

    assert result["checkout_url"] == CHECKOUT_URL
    assert signed_in.sent_keys[0] == signed_in.sent_keys[1]


async def test_an_expired_quote_still_replays_the_link_it_already_produced(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    quote_service: PurchaseQuoteService,
    signed_in: Backend,
) -> None:
    """A quote's TTL must not strand a user who is mid-payment.

    The quote expiring says nothing about the payment page: the user may have the link open
    in front of them. So the replay is checked *before* the lifecycle gates, and the same link
    comes back rather than an expiry error.
    """
    reference = await prepare(purchase_service)
    first = await card_service.create_card_checkout(quote_reference=reference)
    stored = quote_service._store._quotes[reference]
    await quote_service._store.save(stored.model_copy(update={"expires_at": stored.created_at}))

    replayed = await card_service.create_card_checkout(quote_reference=reference)

    assert replayed["checkout_url"] == first["checkout_url"]
    assert replayed["replayed"] is True
    assert len(signed_in.checkout_calls) == 1


async def test_an_expired_quote_whose_payment_settled_can_still_be_checked(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    quote_service: PurchaseQuoteService,
    signed_in: Backend,
) -> None:
    """Checking a payment never depends on the quote still being alive."""
    reference = await prepare(purchase_service)
    await card_service.create_card_checkout(quote_reference=reference)
    stored = quote_service._store._quotes[reference]
    await quote_service._store.save(stored.model_copy(update={"expires_at": stored.created_at}))
    signed_in.stub_status(httpx.Response(200, json=envelope(card_status_payload(status="COMPLETED"))))

    result = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert result["paid"] is True
    assert result["is_final"] is True


async def test_a_terminal_refusal_is_replayed_rather_than_re_sent(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_checkout(failure_response(422, title="MCP_VALIDATION_FAILED"))

    with pytest.raises(CardCheckoutRejectedError):
        await card_service.create_card_checkout(quote_reference=reference)
    with pytest.raises(CardCheckoutRejectedError):
        await card_service.create_card_checkout(quote_reference=reference)

    assert len(signed_in.checkout_calls) == 1


async def test_a_conflicting_answer_is_terminal_and_names_the_recovery(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_checkout(failure_response(409, title="MCP_IDEMPOTENCY_CONFLICT"))

    with pytest.raises(IdempotencyConflictError) as excinfo:
        await card_service.create_card_checkout(quote_reference=reference)

    assert "nothing was charged" in str(excinfo.value).lower()
    assert "prepare the plan again" in str(excinfo.value).lower()


async def test_an_in_progress_answer_is_unresolved_not_a_conflict(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_checkout(failure_response(409, title="MCP_CHECKOUT_IN_PROGRESS"))

    with pytest.raises(CardCheckoutOutcomeUnknownError):
        await card_service.create_card_checkout(quote_reference=reference)

    # Unresolved, not terminal: the same key may be presented again.
    signed_in.stub_checkout(httpx.Response(200, json=envelope(card_checkout_payload())))
    result = await card_service.create_card_checkout(quote_reference=reference)
    assert result["checkout_url"] == CHECKOUT_URL


async def test_an_unreadable_success_envelope_is_unknown_not_a_page(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    signed_in.stub_checkout(httpx.Response(200, content=b"not json at all"))

    with pytest.raises(CardCheckoutOutcomeUnknownError):
        await open_checkout(purchase_service, card_service)


# ------------------------------------------------------------------------ link safety


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "http://checkout.test/pay/1",
        "data:text/html,<script>",
        # Assembled rather than written out: a committed literal with credentials in it would
        # (rightly) trip the repository-hygiene guards in tests/test_repository_hygiene.py.
        "https://{}@checkout.test/pay/1".format("user:secret"),
        "https:///pay/1",
        "not a url at all",
        "https://checkout.test/pay/1 with space",
    ],
)
async def test_an_unsafe_link_is_never_passed_on(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    link: str,
) -> None:
    signed_in.stub_checkout(httpx.Response(200, json=envelope(card_checkout_payload(checkout_url=link))))

    with pytest.raises(UnsafeCheckoutLinkError) as excinfo:
        await open_checkout(purchase_service, card_service)

    assert link not in str(excinfo.value)


async def test_a_missing_link_is_refused(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    signed_in.stub_checkout(httpx.Response(200, json=envelope(card_checkout_payload(checkout_url=None))))

    with pytest.raises(UnsafeCheckoutLinkError):
        await open_checkout(purchase_service, card_service)


@pytest.mark.parametrize("reference", [None, "", "has/slash", "has space", "a" * 200])
async def test_a_page_this_server_could_not_track_is_never_handed_over(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    reference: str | None,
) -> None:
    """Without a usable reference nobody could ever confirm the payment, so no link is shown."""
    signed_in.stub_checkout(httpx.Response(200, json=envelope(card_checkout_payload(payment_reference=reference))))

    with pytest.raises(CardCheckoutRejectedError) as excinfo:
        await open_checkout(purchase_service, card_service)

    assert "nothing was charged" in str(excinfo.value).lower()


def test_the_link_validator_accepts_a_normal_hosted_page() -> None:
    assert safe_checkout_url(CHECKOUT_URL) == CHECKOUT_URL
    assert safe_checkout_url("  " + CHECKOUT_URL + "  ") == CHECKOUT_URL


# ---------------------------------------------------------------------- payment status


async def test_a_payment_reference_from_another_client_is_not_found(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    make_card_service: Callable[[ClientIdentityProvider], CardPaymentService],
    identity_b: StubIdentityProvider,
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
) -> None:
    """Ownership isolation: client B may not read, or even confirm the existence of, A's payment."""
    await open_checkout(purchase_service, card_service)

    await sign_in(make_service(identity_b))
    with pytest.raises(CardPaymentNotFoundError):
        await make_card_service(identity_b).check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert not signed_in.status_calls


async def test_a_reference_this_server_never_issued_is_not_found(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    await open_checkout(purchase_service, card_service)

    with pytest.raises(CardPaymentNotFoundError):
        await card_service.check_card_payment_status(payment_reference="some-other-reference")

    assert not signed_in.status_calls


@pytest.mark.parametrize("reference", ["", "   ", "../../etc/passwd", "ref with space", "a" * 200])
async def test_a_malformed_reference_never_becomes_a_url(
    card_service: CardPaymentService, signed_in: Backend, reference: str
) -> None:
    with pytest.raises(InvalidInputError):
        await card_service.check_card_payment_status(payment_reference=reference)

    assert not signed_in.status_calls


#: Every normalized status the backend documents, and what the result must say about it.
#: ``paid`` and ``provisioned`` are separate columns on purpose: money arriving and an eSIM
#: existing are different facts, and no row is allowed to conflate them.
STATUS_MATRIX = [
    # status,         paid,  is_final, new_checkout_required
    ("PENDING", False, False, False),
    ("PAID", True, False, False),
    ("PROVISIONING", True, False, False),
    ("COMPLETED", True, True, False),
    ("FAILED", False, True, True),
    ("EXPIRED", False, True, True),
    ("CANCELLED", False, True, True),
]


@pytest.mark.parametrize(("status", "paid", "is_final", "needs_new"), STATUS_MATRIX)
async def test_every_status_maps_to_the_right_facts(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    status: str,
    paid: bool,
    is_final: bool,
    needs_new: bool,
) -> None:
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status(httpx.Response(200, json=envelope(card_status_payload(status=status))))

    result = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert result["payment_status"] == status
    assert result["status"] == status.lower()
    assert result["paid"] is paid
    assert result["charged"] is paid
    assert result["is_final"] is is_final
    assert result.get("new_checkout_required", False) is needs_new
    assert result["next_step"]


@pytest.mark.parametrize(("status", "paid", "is_final", "needs_new"), STATUS_MATRIX)
async def test_the_next_action_is_passed_through_verbatim_for_every_status(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    status: str,
    paid: bool,
    is_final: bool,
    needs_new: bool,
) -> None:
    """The platform owns the eSIM-retrieval instruction; this server never invents one."""
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status(
        httpx.Response(200, json=envelope(card_status_payload(status=status, next_action="GET_ESIM_BY_ORDER")))
    )

    result = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert result["next_action"] == "GET_ESIM_BY_ORDER"


@pytest.mark.parametrize(("status", "paid", "is_final", "needs_new"), STATUS_MATRIX)
async def test_no_next_action_is_invented_when_the_platform_sends_none(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    status: str,
    paid: bool,
    is_final: bool,
    needs_new: bool,
) -> None:
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status(httpx.Response(200, json=envelope(card_status_payload(status=status, next_action=None))))

    result = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert "next_action" not in result


@pytest.mark.parametrize("status", ["PENDING", "PAID", "PROVISIONING", "COMPLETED"])
async def test_provisioned_is_reported_as_the_platform_reported_it(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    status: str,
) -> None:
    """Never inferred from ``paid``: a paid payment whose eSIM is not ready is not ready."""
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status(httpx.Response(200, json=envelope(card_status_payload(status=status, provisioned=False))))

    result = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert result["provisioned"] is False


async def test_a_pending_payment_tells_the_model_to_wait_and_re_offer_the_link(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    await open_checkout(purchase_service, card_service)

    result = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert result["paid"] is False
    assert result["charged"] is False
    assert result["is_final"] is False
    assert result["checkout_url"] == CHECKOUT_URL
    assert "has not paid yet" in result["next_step"].lower()


@pytest.mark.parametrize("status", ["PAID", "PROVISIONING"])
async def test_money_arrived_but_the_esim_is_not_ready(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    status: str,
) -> None:
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status(httpx.Response(200, json=envelope(card_status_payload(status=status))))

    result = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert result["paid"] is True
    assert result["charged"] is True
    assert result["provisioned"] is False
    assert result["is_final"] is False
    next_step = result["next_step"].lower()
    assert "payment was received" in next_step
    assert "not ready" in next_step or "preparing" in next_step


async def test_a_completed_payment_reports_the_order_and_the_next_action(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status(
        httpx.Response(
            200,
            json=envelope(
                card_status_payload(
                    status="COMPLETED",
                    order_id=ORDER_ID,
                    provisioned=True,
                    next_action="GET_ESIM_BY_ORDER",
                    quote_reference="quote-ref-0001",
                )
            ),
        )
    )

    result = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert result["paid"] is True
    assert result["provisioned"] is True
    assert result["is_final"] is True
    assert result["order_id"] == ORDER_ID
    assert result["next_action"] == "GET_ESIM_BY_ORDER"
    assert result["bundle_code"] == BUNDLE_CODE
    assert result["quote_reference"] == "quote-ref-0001"
    assert result["amount"] == "8.06"
    assert result["currency"] == "USD"
    assert "paid for by card" in result["next_step"].lower()


async def test_a_completed_payment_consumes_the_quote(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    quote_service: PurchaseQuoteService,
    signed_in: Backend,
) -> None:
    """A quote spent on a settled card payment can never become a second order."""
    result = await open_checkout(purchase_service, card_service)
    signed_in.stub_status(httpx.Response(200, json=envelope(card_status_payload(status="COMPLETED"))))

    await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    stored = quote_service._store._quotes[result["quote_reference"]]
    assert stored.status is QuoteStatus.CONSUMED


@pytest.mark.parametrize("status", ["FAILED", "EXPIRED", "CANCELLED"])
async def test_a_failed_payment_says_nothing_was_charged_and_asks_for_a_new_page(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    status: str,
) -> None:
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status(httpx.Response(200, json=envelope(card_status_payload(status=status))))

    result = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert result["paid"] is False
    assert result["charged"] is False
    assert result["provisioned"] is False
    assert result["is_final"] is True
    assert result["new_checkout_required"] is True
    assert "nothing was charged" in result["next_step"].lower()


async def test_an_ambiguous_payment_stops_every_further_check(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status(httpx.Response(200, json=envelope(card_status_payload(status="AMBIGUOUS"))))

    with pytest.raises(CardPaymentAmbiguousError) as excinfo:
        await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)
    message = str(excinfo.value).lower()
    assert "contact esim support" in message
    assert "never tell the user it succeeded or failed" in message

    with pytest.raises(CardPaymentAmbiguousError):
        await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert len(signed_in.status_calls) == 1, "an ambiguous payment was checked again"


async def test_an_ambiguous_payment_stops_a_second_checkout_too(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    result = await open_checkout(purchase_service, card_service)
    signed_in.stub_status(httpx.Response(200, json=envelope(card_status_payload(status="AMBIGUOUS"))))
    with pytest.raises(CardPaymentAmbiguousError):
        await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    with pytest.raises(CardPaymentAmbiguousError):
        await card_service.create_card_checkout(quote_reference=result["quote_reference"])

    assert len(signed_in.checkout_calls) == 1


async def test_a_terminal_status_is_replayed_rather_than_re_read(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status(httpx.Response(200, json=envelope(card_status_payload(status="COMPLETED"))))

    first = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)
    second = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert len(signed_in.status_calls) == 1
    assert second["replayed"] is True
    assert second["payment_status"] == first["payment_status"]


async def test_an_unrecognized_status_word_is_never_guessed_at(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """An unknown word must not degrade to "keep waiting" or to "nothing was taken"."""
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status(httpx.Response(200, json=envelope(card_status_payload(status="WHO_KNOWS"))))

    with pytest.raises(CardPaymentStatusUnavailableError) as excinfo:
        await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert "never guess" in str(excinfo.value).lower()


async def test_a_status_timeout_is_never_an_outcome(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status_error(httpx.ReadTimeout("boom"))

    with pytest.raises(CardPaymentStatusUnavailableError):
        await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)


async def test_status_reads_are_bounded(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """A model that checks forever stops being allowed to, at a documented cap."""
    await open_checkout(purchase_service, card_service)

    for _ in range(MAX_STATUS_CHECKS):
        await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    with pytest.raises(CardPaymentCheckLimitError):
        await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert len(signed_in.status_calls) == MAX_STATUS_CHECKS


async def test_checking_never_polls_by_itself(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """One tool call, one backend read. No retry loop hides inside either tool."""
    await open_checkout(purchase_service, card_service)

    await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    assert len(signed_in.status_calls) == 1


async def test_the_status_tool_has_no_way_to_be_told_a_payment_succeeded() -> None:
    """A redirect cannot become evidence if there is no argument through which to claim it."""
    import inspect

    parameters = set(inspect.signature(CardPaymentService.check_card_payment_status).parameters)

    assert parameters == {"self", "payment_reference", "ctx"}
    for forbidden in ("paid", "success", "redirect", "returned", "confirmed", "amount", "status"):
        assert forbidden not in parameters


@pytest.mark.parametrize(
    ("status", "title", "error_type"),
    [
        (401, "UNAUTHORIZED", AuthenticationRequiredError),
        (429, "TOO_MANY_REQUESTS", RateLimitedError),
        (500, "INTERNAL", CardPaymentStatusUnavailableError),
        (404, "NOT_FOUND", CardPaymentStatusUnavailableError),
    ],
)
async def test_a_failed_status_read_never_claims_an_outcome(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    status: int,
    title: str,
    error_type: type[Exception],
) -> None:
    await open_checkout(purchase_service, card_service)
    signed_in.stub_status(failure_response(status, title=title))

    with pytest.raises(error_type) as excinfo:
        await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    message = str(excinfo.value).lower()
    assert "succeeded" not in message or "never" in message
    assert "was charged" not in message


# ------------------------------------------------------------------- secrets and privacy


async def test_no_provider_secret_ever_reaches_a_result(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """The payload carries a provider session id and a client secret; neither may survive."""
    opened = await open_checkout(purchase_service, card_service)
    pending = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    for result in (opened, pending):
        blob = rendered(result)
        for secret in PROVIDER_SECRETS:
            assert secret not in blob, f"a provider secret reached a result: {secret}"
        assert "eyJ" not in blob, "a JWT reached a tool result"
        assert "refresh-token-value" not in blob
        assert "access_token" not in blob
        assert "refresh_token" not in blob
        assert "Idempotency-Key" not in blob
        assert "idempotency_key" not in blob


async def test_no_result_mentions_a_card_detail_field(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    opened = await open_checkout(purchase_service, card_service)

    blob = rendered(opened).lower()
    for forbidden in ("card_number", "cardnumber", "cvv", "cvc", "expiry_month", "pan", "cardholder"):
        assert forbidden not in blob


async def test_the_raw_idempotency_key_never_reaches_a_log(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await open_checkout(purchase_service, card_service)

    key = signed_in.sent_keys[0]
    written = "\n".join(
        [record.getMessage() for record in caplog.records] + [rendered(record.__dict__) for record in caplog.records]
    )
    assert key not in written
    for secret in PROVIDER_SECRETS:
        assert secret not in written


async def test_no_log_record_claims_a_charge_while_only_a_page_exists(
    purchase_service: PurchasePreparationService,
    card_service: CardPaymentService,
    signed_in: Backend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await open_checkout(purchase_service, card_service)

    for record in caplog.records:
        assert getattr(record, "charged", False) is False
        assert getattr(record, "order_created", False) is False


# ---------------------------------------------------------- the wallet path is untouched


async def test_a_card_quote_still_cannot_be_confirmed_from_the_wallet_tool(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    card_service: CardPaymentService,
    signed_in: Backend,
) -> None:
    """The two payment methods stay separated in both directions."""
    reference = await prepare(purchase_service, payment_method="Card")

    with pytest.raises(NonWalletQuoteError) as excinfo:
        await confirmation_service.confirm_purchase(quote_reference=reference)

    assert "create_card_checkout" in str(excinfo.value)
    assert not signed_in.checkout_calls


async def test_opening_a_checkout_never_touches_the_wallet_purchase_route(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """The wallet route is not even mocked here, so reaching it would fail the request."""
    await open_checkout(purchase_service, card_service)

    touched = [str(call.request.url) for call in signed_in.router.calls]
    assert not any("bundle/assign" in url for url in touched)


# ----------------------------------------------------------------------- transport guard


def test_the_card_status_route_refuses_anything_but_one_plain_reference() -> None:
    from esim_mcp.client.base import enforce_route_is_permitted

    enforce_route_is_permitted("GET", "/mcp/user/bundle/card/status/pay-ref-0001")

    for refused in (
        "/mcp/user/bundle/card/status/",
        "/mcp/user/bundle/card/status/a/b",
        "/mcp/user/bundle/card/status/../../wallet/top-up",
        "/mcp/user/bundle/card/status/ref?x=1",
    ):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted("GET", refused)


def test_the_card_status_route_is_readable_but_not_writable() -> None:
    from esim_mcp.client.base import enforce_route_is_permitted

    with pytest.raises(ForbiddenBackendRouteError):
        enforce_route_is_permitted("POST", "/mcp/user/bundle/card/status/pay-ref-0001")


def test_the_card_checkout_route_is_reachable_by_post_only() -> None:
    from esim_mcp.client.base import enforce_route_is_permitted

    enforce_route_is_permitted("POST", CHECKOUT_PATH)

    for method in ("PUT", "PATCH", "DELETE"):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted(method, CHECKOUT_PATH)


@pytest.mark.parametrize(
    "status",
    [status.value for status in CardPaymentStatus],
)
def test_every_payment_status_has_guidance_for_the_model(status: str) -> None:
    """A state with no instruction attached is a state the model will improvise around."""
    from esim_mcp.tools.card_checkout import _STATUS_GUIDANCE

    typed = CardPaymentStatus(status)
    if typed is CardPaymentStatus.AMBIGUOUS:
        # Ambiguity is raised as an error rather than returned, so it carries its own wording.
        assert typed not in _STATUS_GUIDANCE
        return
    assert _STATUS_GUIDANCE[typed]


# ------------------------------------------------------------------ through the real binding
#
# Everything above drives the service class. These drive the *registered MCP tools*, so a
# registration mistake -- a wrong argument name, a lost annotation, a tool that was never
# bound -- fails here rather than in QA.


def payload_of(result: Any) -> dict[str, Any]:
    """The structured result a client receives, however this SDK version carries it."""
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("tool returned no readable content")


async def test_the_documented_chat_flow_works_end_to_end(settings: Settings, backend: Backend) -> None:
    """login -> browse -> prepare Card -> checkout -> pay -> check -> COMPLETED."""
    from esim_mcp.server import build_components

    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    server = components.server
    try:
        await server.call_tool("request_login_otp", {"email": "person@example.com"})
        await server.call_tool("verify_login_otp", {"verification_pin": "123456", "email": "person@example.com"})

        prepared = payload_of(
            await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Card"})
        )
        assert prepared["payment_method"] == "Card"
        assert prepared["charged"] is False

        opened = payload_of(await server.call_tool("create_card_checkout", {"quote_reference": prepared["quote_id"]}))
        assert opened["checkout_url"] == CHECKOUT_URL
        assert opened["paid"] is False

        # The user pays on the hosted page; the platform's answer is what changes, not ours.
        backend.stub_status(httpx.Response(200, json=envelope(card_status_payload(status="COMPLETED"))))
        settled = payload_of(
            await server.call_tool("check_card_payment_status", {"payment_reference": opened["payment_reference"]})
        )
    finally:
        await components.aclose()

    assert settled["paid"] is True
    assert settled["is_final"] is True
    assert settled["order_id"] == ORDER_ID


async def test_the_flow_never_touches_a_forbidden_path(settings: Settings, backend: Backend) -> None:
    from esim_mcp.server import build_components

    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    server = components.server
    try:
        await server.call_tool("request_login_otp", {"email": "person@example.com"})
        await server.call_tool("verify_login_otp", {"verification_pin": "123456", "email": "person@example.com"})
        prepared = payload_of(
            await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Card"})
        )
        opened = payload_of(await server.call_tool("create_card_checkout", {"quote_reference": prepared["quote_id"]}))
        await server.call_tool("check_card_payment_status", {"payment_reference": opened["payment_reference"]})
    finally:
        await components.aclose()

    touched = [str(call.request.url).lower() for call in backend.router.calls]
    for banned in ("bundle/assign", "assign-top-up", "wallet/top-up", "voucher", "promo", "callback", "refund"):
        assert not any(banned in url for url in touched), f"a request reached {banned!r}"

    # Exactly the documented card routes, and the reads the flow needs.
    paths = {call.request.url.path for call in backend.router.calls}
    assert f"/api/v1{CHECKOUT_PATH}" in paths
    assert f"/api/v1/mcp/user/bundle/card/status/{PAYMENT_REFERENCE}" in paths


async def test_the_two_card_tools_are_reachable_through_the_binding(settings: Settings, backend: Backend) -> None:
    from esim_mcp.server import build_components

    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        names = {tool.name for tool in await components.server.list_tools()}
    finally:
        await components.aclose()

    assert {"create_card_checkout", "check_card_payment_status"} <= names


async def test_a_status_read_asks_in_the_currency_the_user_was_quoted(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """An amount read back to the user must be the one they agreed to, not a re-conversion."""
    await open_checkout(purchase_service, card_service)

    await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    headers = signed_in.status_calls[0].headers
    assert headers["X-Currency"] == "USD"
    assert headers["Accept-Language"] == "en"
    assert headers["Authorization"].startswith("Bearer ")


# ------------------------------------------------------------------- the webhook boundary
#
# Stripe calls the backend's existing signature-verified payment webhook; the webhook settles
# the payment and triggers provisioning. This server is a *reader* of that outcome. These
# assert it has no way to be anything else.


WEBHOOK_ROUTE = "/callback/payment-webhook"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_the_payment_webhook_is_unreachable_from_this_server(method: str) -> None:
    from esim_mcp.client.base import enforce_route_is_permitted

    with pytest.raises(ForbiddenBackendRouteError):
        enforce_route_is_permitted(method, WEBHOOK_ROUTE)


def test_no_source_file_builds_the_payment_webhook_route() -> None:
    """Naming it in prose is fine. Building it as a path is not."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in (root / "src").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if f'"{WEBHOOK_ROUTE}"' in stripped or f"'{WEBHOOK_ROUTE}'" in stripped:
                offenders.append(f"{path.name}: {stripped}")

    assert not offenders, f"the webhook route is built as a path: {offenders}"


async def test_nothing_in_the_card_flow_can_settle_a_payment(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """The whole flow issues exactly two request kinds, and neither settles anything."""
    await open_checkout(purchase_service, card_service)
    await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    card_requests = [call.request for call in signed_in.router.calls if "/card/" in call.request.url.path]
    assert {(request.method, request.url.path) for request in card_requests} == {
        ("POST", f"/api/v1{CHECKOUT_PATH}"),
        ("GET", f"/api/v1/mcp/user/bundle/card/status/{PAYMENT_REFERENCE}"),
    }
    touched = [str(call.request.url).lower() for call in signed_in.router.calls]
    for banned in ("webhook", "callback", "capture", "confirm", "refund", "provision"):
        assert not any(banned in url for url in touched), f"a request reached {banned!r}"


async def test_a_pending_payment_stays_pending_however_often_it_is_read(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """Reading a payment cannot advance it. Only the webhook can, and it is not us."""
    await open_checkout(purchase_service, card_service)

    for _ in range(3):
        result = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)
        assert result["paid"] is False
        assert result["payment_status"] == "PENDING"


# ------------------------------------------------------ developerMessage and envelope prose


async def test_no_backend_prose_reaches_a_card_result(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    """`developerMessage` and the envelope's own prose are for the platform, not the model."""
    signed_in.stub_checkout(
        httpx.Response(
            200,
            json=envelope(
                card_checkout_payload(message="backend prose 0002"),
                message="localized envelope text 0003",
                developer_message="internal stack detail 0004",
            ),
        )
    )
    signed_in.stub_status(
        httpx.Response(
            200,
            json=envelope(
                card_status_payload(message="backend prose 0005"),
                message="localized envelope text 0006",
                developer_message="internal stack detail 0007",
            ),
        )
    )

    opened = await open_checkout(purchase_service, card_service)
    checked = await card_service.check_card_payment_status(payment_reference=PAYMENT_REFERENCE)

    for result in (opened, checked):
        blob = rendered(result)
        for prose in (
            "backend prose 0002",
            "localized envelope text 0003",
            "internal stack detail 0004",
            "backend prose 0005",
            "localized envelope text 0006",
            "internal stack detail 0007",
            "internal backend detail 0001",
        ):
            assert prose not in blob, f"backend prose reached a result: {prose}"
        assert "developerMessage" not in blob
        assert "developer_message" not in blob


async def test_no_backend_prose_reaches_a_card_error(
    purchase_service: PurchasePreparationService, card_service: CardPaymentService, signed_in: Backend
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_checkout(failure_response(422, title="MCP_VALIDATION_FAILED"))

    with pytest.raises(CardCheckoutRejectedError) as excinfo:
        await card_service.create_card_checkout(quote_reference=reference)

    message = str(excinfo.value)
    assert "localized text the client never sees" not in message
    assert "MCP_VALIDATION_FAILED" not in message


# --------------------------------------------------- the wallet purchase is not regressed


def test_the_wallet_purchase_body_still_carries_its_payment_type() -> None:
    """Removing `payment_type` from the *card* body must not have touched the wallet one.

    The two routes have different contracts: the wallet endpoint takes `payment_type`, the
    card endpoint forbids it. Asserted here because the change that aligned one of them is
    exactly the change that could have broken the other.
    """
    from esim_mcp.client.purchase import WALLET_PAYMENT_TYPE

    assert WALLET_PAYMENT_TYPE == "Wallet"

    import inspect

    from esim_mcp.client.purchase import PurchaseApiClient

    source = inspect.getsource(PurchaseApiClient.purchase_bundle_with_wallet)
    assert '"payment_type": WALLET_PAYMENT_TYPE' in source


def test_the_two_purchase_routes_are_still_distinct() -> None:
    from esim_mcp.client.card import CARD_CHECKOUT_PATH, CARD_STATUS_PATH_PREFIX
    from esim_mcp.client.purchase import MCP_PURCHASE_PATH

    assert MCP_PURCHASE_PATH == "/mcp/user/bundle/assign"
    assert CARD_CHECKOUT_PATH == "/mcp/user/bundle/card/checkout"
    assert CARD_STATUS_PATH_PREFIX == "/mcp/user/bundle/card/status/"
    assert MCP_PURCHASE_PATH != CARD_CHECKOUT_PATH
