"""``confirm_purchase`` against a mocked backend. No test here reaches QA and none spends money.

Everything is stubbed with ``respx``: the only mocked mutating route is the MCP purchase
route, and a call to any *other* route surfaces as an unmocked-request error rather than
passing silently. That is what keeps "this suite cannot buy anything" a property of the
harness rather than a promise in a comment.

The properties asserted here are the ones that cost real money if they regress:

* a purchase needs a session, an owned quote, a usable quote and a wallet quote;
* one idempotency key is minted per quote and **reused** for every later attempt;
* a repeated confirmation replays the first purchase instead of making a second;
* a timeout is never reported as a failure and never re-keyed;
* the quote is consumed only after a confirmed, completed purchase;
* no token, key or backend internal ever reaches a result, an error or a log.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx

from esim_mcp.errors import (
    AuthenticationRequiredError,
    BundleUnavailableError,
    EsimMcpError,
    IdempotencyConflictError,
    InsufficientWalletBalanceError,
    InvalidInputError,
    NonWalletQuoteError,
    PurchaseAttemptLimitError,
    PurchaseCurrencyMismatchError,
    PurchaseInProgressError,
    PurchaseManualInterventionError,
    PurchaseOutcomeUnknownError,
    PurchaseRejectedError,
    PurchaseRouteUnavailableError,
    PurchaseUnavailableError,
    QuoteCancelledError,
    QuoteExpiredError,
    QuoteNotFoundError,
    RateLimitedError,
)
from esim_mcp.purchase.execution import (
    MAX_EXECUTION_ATTEMPTS,
    ExecutionStatus,
    PurchaseExecutionService,
    new_idempotency_key,
)
from esim_mcp.purchase.models import QuoteStatus
from esim_mcp.purchase.service import PurchaseQuoteService
from esim_mcp.purchase.store import QuoteOwner
from esim_mcp.purchase.validation import new_quote_id
from esim_mcp.session.identity import ClientIdentityProvider
from esim_mcp.tools.authentication import AuthenticationService
from esim_mcp.tools.purchase_execution import PurchaseConfirmationService
from esim_mcp.tools.purchase_preparation import PurchasePreparationService, user_ref_of
from tests.conftest import (
    API_URL,
    CATALOG_COUNTRIES,
    CATALOG_REGIONS,
    StubIdentityProvider,
    bundle_payload,
    envelope,
    mock_login_routes,
    purchase_result_payload,
    sign_in,
    wallet_payload,
)

BUNDLE_CODE = "aaaaaaaa-0000-4000-8000-000000000001"
PURCHASE_URL = f"{API_URL}/mcp/user/bundle/assign"
WALLET_PATH = f"{API_URL}/wallet/user_wallet_by_user"

#: The backend's accepted idempotency-key alphabet and length window.
KEY_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")
KEY_MIN_LENGTH = 32
KEY_MAX_LENGTH = 128


def rendered(value: Any) -> str:
    return json.dumps(value, default=str)


@dataclass(slots=True)
class Backend:
    """The mocked backend, with a handle to the one route that can spend money."""

    router: respx.Router
    purchase: respx.Route
    bundle: respx.Route
    wallet: respx.Route

    @property
    def purchase_calls(self) -> list[httpx.Request]:
        return [call.request for call in self.router.calls if call.request.url.path.endswith("/mcp/user/bundle/assign")]

    @property
    def sent_keys(self) -> list[str]:
        return [request.headers["Idempotency-Key"] for request in self.purchase_calls]

    def stub_purchase(self, response: httpx.Response) -> None:
        self.purchase.mock(return_value=response)

    def stub_purchase_error(self, error: Exception) -> None:
        self.purchase.mock(side_effect=error)

    def stub_bundle(self, payload: dict[str, Any]) -> None:
        self.bundle.mock(return_value=httpx.Response(200, json=envelope(payload)))

    def stub_wallet(self, payload: dict[str, Any] | None) -> None:
        self.wallet.mock(return_value=httpx.Response(200, json=envelope(payload)))


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
    purchase = respx_mock.post(PURCHASE_URL).mock(
        return_value=httpx.Response(200, json=envelope(purchase_result_payload()))
    )
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
    return Backend(router=respx_mock, purchase=purchase, bundle=bundle, wallet=wallet)


@pytest.fixture
async def signed_in(service: AuthenticationService, backend: Backend) -> Backend:
    await sign_in(service)
    return backend


async def prepare(
    purchase_service: PurchasePreparationService,
    *,
    payment_method: str = "Wallet",
    country: str | None = None,
    region: str | None = None,
) -> str:
    """Prepare a quote through the real preparation tool and return its reference."""
    result = await purchase_service.prepare_purchase(
        bundle_code=BUNDLE_CODE, payment_method=payment_method, country=country, region=region
    )
    return result["quote_id"]


# ------------------------------------------------------------------------ authentication


async def test_confirming_requires_an_authenticated_user(
    confirmation_service: PurchaseConfirmationService, backend: Backend
) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await confirmation_service.confirm_purchase(quote_reference="anything")

    assert not backend.purchase_calls, "an unauthenticated call reached the purchase route"


async def test_a_signed_out_user_cannot_confirm_a_quote_prepared_before_signing_out(
    service: AuthenticationService,
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """A quote -- and its idempotency key -- must not outlive the session that made it."""
    reference = await prepare(purchase_service)
    await service.logout()

    with pytest.raises(AuthenticationRequiredError):
        await confirmation_service.confirm_purchase(quote_reference=reference)

    assert not signed_in.purchase_calls


# ------------------------------------------------------------------------------- quotes


async def test_an_unknown_quote_reference_is_refused(
    confirmation_service: PurchaseConfirmationService, signed_in: Backend
) -> None:
    with pytest.raises(QuoteNotFoundError):
        await confirmation_service.confirm_purchase(quote_reference="no-such-quote")

    assert not signed_in.purchase_calls


@pytest.mark.parametrize("reference", ["", "   "])
async def test_a_blank_quote_reference_is_refused_with_an_actionable_message(
    confirmation_service: PurchaseConfirmationService, signed_in: Backend, reference: str
) -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        await confirmation_service.confirm_purchase(quote_reference=reference)

    message = str(excinfo.value).lower()
    assert "never invent one" in message
    assert not signed_in.purchase_calls


async def test_another_clients_quote_cannot_be_confirmed(
    make_purchase_service: Callable[[ClientIdentityProvider], PurchasePreparationService],
    make_confirmation_service: Callable[[ClientIdentityProvider], PurchaseConfirmationService],
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    backend: Backend,
) -> None:
    """A foreign quote is invisible, not merely refused -- and it is certainly not bought."""
    await sign_in(make_service(identity_a))
    reference = await prepare(make_purchase_service(identity_a))

    await sign_in(make_service(identity_b), email="other@example.com")

    with pytest.raises(QuoteNotFoundError):
        await make_confirmation_service(identity_b).confirm_purchase(quote_reference=reference)

    assert not backend.purchase_calls


async def test_an_expired_quote_cannot_be_confirmed(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    quote_service: PurchaseQuoteService,
    signed_in: Backend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its price and balance are stale, so the amount the user agreed to is no longer real."""
    reference = await prepare(purchase_service)

    from esim_mcp.purchase import models as quote_models

    later = quote_models.utc_now().timestamp() + 10_000
    monkeypatch.setattr(
        quote_models,
        "utc_now",
        lambda: quote_models.datetime.fromtimestamp(later, tz=quote_models.UTC),
    )

    with pytest.raises(QuoteExpiredError):
        await confirmation_service.confirm_purchase(quote_reference=reference)

    assert not signed_in.purchase_calls


async def test_a_cancelled_quote_cannot_be_confirmed(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service)
    await purchase_service.cancel_prepared_purchase(quote_id=reference)

    with pytest.raises(QuoteCancelledError):
        await confirmation_service.confirm_purchase(quote_reference=reference)

    assert not signed_in.purchase_calls


async def test_a_card_quote_cannot_be_confirmed(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """Wallet only in this phase, and the refusal must never reach the platform."""
    reference = await prepare(purchase_service, payment_method="Card")

    with pytest.raises(NonWalletQuoteError) as excinfo:
        await confirmation_service.confirm_purchase(quote_reference=reference)

    assert "wallet" in str(excinfo.value).lower()
    assert not signed_in.purchase_calls


async def test_a_quote_whose_balance_did_not_cover_the_plan_is_refused_locally(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """Preparation still produces a quote so the shortfall can be explained; buying refuses it."""
    signed_in.stub_wallet(wallet_payload(balance=1.0))
    reference = await prepare(purchase_service)

    with pytest.raises(InsufficientWalletBalanceError):
        await confirmation_service.confirm_purchase(quote_reference=reference)

    assert not signed_in.purchase_calls


# ------------------------------------------------------------------------ the happy path


async def test_a_wallet_purchase_completes_and_reports_only_safe_facts(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service)

    result = await confirmation_service.confirm_purchase(quote_reference=reference)

    assert result["status"] == "purchased"
    assert result["order_created"] is True
    assert result["charged"] is True
    assert result["order_id"] == "ord-0001-aaaa-bbbb"
    assert result["order_status"] == "SUCCESS"
    assert result["payment_status"] == "COMPLETED"
    assert result["provisioning_status"] == "COMPLETED"
    assert result["next_state"] == "GET_ESIM_BY_ORDER"
    assert result["payment_method"] == "Wallet"
    assert result["bundle"]["bundle_code"] == BUNDLE_CODE
    assert result["bundle"]["name"] == "France 5GB / 30 Days"
    assert result["pricing"] == {"quoted_amount": "8.06", "currency": "USD"}
    assert result["replayed"] is False
    assert len(signed_in.purchase_calls) == 1


async def test_the_request_is_built_from_the_stored_quote_and_nothing_else(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """Field for field against the platform's own contract."""
    reference = await prepare(purchase_service, country="France")

    await confirmation_service.confirm_purchase(quote_reference=reference)

    body = json.loads(signed_in.purchase_calls[0].content)
    assert body["bundle_code"] == BUNDLE_CODE
    assert body["payment_type"] == "Wallet"
    assert body["quote_reference"] == reference
    # related_search comes from the platform's own country list, captured at preparation.
    assert body["related_search"] == {"countries": [{"iso3_code": "FRA", "country_name": "France"}]}
    # Nothing priced, nothing identifying, nothing the platform derives itself.
    for forbidden in ("price", "amount", "balance", "tax", "user_id", "email", "device_id", "promo_code"):
        assert forbidden not in body


async def test_a_region_quote_sends_the_region_shape_of_related_search(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service, region="Europe")

    await confirmation_service.confirm_purchase(quote_reference=reference)

    body = json.loads(signed_in.purchase_calls[0].content)
    assert body["related_search"] == {"region": {"iso_code": "EUR", "region_name": "Europe"}}


async def test_a_quote_with_no_destination_context_omits_related_search(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """Optional by contract: a half-filled context would be worse than none."""
    reference = await prepare(purchase_service)

    await confirmation_service.confirm_purchase(quote_reference=reference)

    assert "related_search" not in json.loads(signed_in.purchase_calls[0].content)


async def test_the_request_carries_every_header_the_platform_requires(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service)

    await confirmation_service.confirm_purchase(quote_reference=reference)

    headers = signed_in.purchase_calls[0].headers
    assert headers["Authorization"].startswith("Bearer ")
    assert headers["X-Device-Id"]
    assert headers["Accept-Language"] == "en"
    assert headers["X-Currency"] == "USD"
    assert headers["Idempotency-Key"]


async def test_the_generated_idempotency_key_matches_the_platforms_contract(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """Wrong alphabet or wrong length and every purchase would be rejected at the door."""
    reference = await prepare(purchase_service)

    await confirmation_service.confirm_purchase(quote_reference=reference)

    key = signed_in.sent_keys[0]
    assert KEY_MIN_LENGTH <= len(key) <= KEY_MAX_LENGTH
    assert set(key) <= KEY_ALPHABET


def test_generated_keys_are_random_and_never_derived_from_the_quote() -> None:
    keys = {new_idempotency_key() for _ in range(50)}

    assert len(keys) == 50


#: The platform's published patterns for the two identifiers this server generates. Pinned
#: here because a value that fails one of them is rejected at the door -- every purchase
#: would fail, and only against a real backend, which no mocked test would ever notice.
BACKEND_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~\-]+$")
BACKEND_QUOTE_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]+$")
BACKEND_QUOTE_REFERENCE_MAX_LENGTH = 128


def test_every_generated_identifier_satisfies_the_platforms_own_pattern() -> None:
    for _ in range(200):
        key = new_idempotency_key()
        assert BACKEND_IDEMPOTENCY_KEY_PATTERN.match(key), key
        assert KEY_MIN_LENGTH <= len(key) <= KEY_MAX_LENGTH

        reference = new_quote_id()
        assert BACKEND_QUOTE_REFERENCE_PATTERN.match(reference), reference
        assert len(reference) <= BACKEND_QUOTE_REFERENCE_MAX_LENGTH


# --------------------------------------------------------------------- quote lifecycle


async def test_the_quote_is_consumed_only_after_a_successful_purchase(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    quote_service: PurchaseQuoteService,
    session_manager: Any,
    identity_a: StubIdentityProvider,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service)
    owner = await owner_of(session_manager, identity_a)

    before = await quote_service.get_record(owner, reference)
    assert before.status is QuoteStatus.ACTIVE

    await confirmation_service.confirm_purchase(quote_reference=reference)

    after = await quote_service.get_record(owner, reference)
    assert after.status is QuoteStatus.CONSUMED


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (failure_response(400, title="INSUFFICIENT_WALLET_BALANCE"), InsufficientWalletBalanceError),
        (failure_response(422, title="VALIDATION_ERROR"), PurchaseRejectedError),
        (failure_response(424, title="MCP_MANUAL_INTERVENTION_REQUIRED"), PurchaseManualInterventionError),
    ],
)
async def test_a_quote_is_never_consumed_when_the_purchase_did_not_complete(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    quote_service: PurchaseQuoteService,
    session_manager: Any,
    identity_a: StubIdentityProvider,
    signed_in: Backend,
    response: httpx.Response,
    expected: type[EsimMcpError],
) -> None:
    """Consuming a quote whose purchase may not have happened destroys the only record of it."""
    reference = await prepare(purchase_service)
    signed_in.stub_purchase(response)
    owner = await owner_of(session_manager, identity_a)

    with pytest.raises(expected):
        await confirmation_service.confirm_purchase(quote_reference=reference)

    stored = await quote_service.get_record(owner, reference)
    assert stored.status is not QuoteStatus.CONSUMED


async def owner_of(session_manager: Any, identity_provider: StubIdentityProvider) -> QuoteOwner:
    """Rebuild the owner key exactly as the tools build it."""
    identity = identity_provider.identity
    session = await session_manager.require_session(identity.session_key)
    return QuoteOwner(session_key=identity.session_key, user_ref=user_ref_of(session))


# ---------------------------------------------------------------------------- replay


async def test_confirming_the_same_quote_twice_never_buys_it_twice(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """The single most expensive regression this suite can catch."""
    reference = await prepare(purchase_service)

    first = await confirmation_service.confirm_purchase(quote_reference=reference)
    second = await confirmation_service.confirm_purchase(quote_reference=reference)

    assert len(signed_in.purchase_calls) == 1, "a second purchase request was sent"
    assert second["order_id"] == first["order_id"]
    assert second["replayed"] is True
    assert "never tell them they bought it twice" in second["replay_note"].lower()


async def test_a_replayed_result_reports_the_same_order_and_no_second_charge(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service)
    await confirmation_service.confirm_purchase(quote_reference=reference)

    replayed = await confirmation_service.confirm_purchase(quote_reference=reference)

    assert replayed["status"] == "purchased"
    assert replayed["order_created"] is True
    assert replayed["charged"] is True
    assert replayed["pricing"]["quoted_amount"] == "8.06"


async def test_a_platform_side_replay_is_reported_as_a_replay_not_a_new_purchase(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """The platform recognized our key and replayed its own stored answer."""
    reference = await prepare(purchase_service)
    signed_in.stub_purchase(httpx.Response(200, json=envelope(purchase_result_payload(idempotent_replay=True))))

    result = await confirmation_service.confirm_purchase(quote_reference=reference)

    assert result["replayed"] is True
    assert result["order_created"] is True


async def test_a_terminal_failure_is_replayed_rather_than_retried(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_purchase(failure_response(422, title="VALIDATION_ERROR"))

    with pytest.raises(PurchaseRejectedError):
        await confirmation_service.confirm_purchase(quote_reference=reference)
    with pytest.raises(PurchaseRejectedError):
        await confirmation_service.confirm_purchase(quote_reference=reference)

    assert len(signed_in.purchase_calls) == 1, "a refused purchase was sent again"


# ------------------------------------------------------------------- idempotency keys


async def test_the_same_key_is_reused_across_a_safe_transport_retry(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """The property the whole phase rests on: a retry must never mint a new key."""
    reference = await prepare(purchase_service)
    signed_in.stub_purchase_error(httpx.ConnectError("connection reset"))

    with pytest.raises(PurchaseOutcomeUnknownError):
        await confirmation_service.confirm_purchase(quote_reference=reference)

    signed_in.stub_purchase(httpx.Response(200, json=envelope(purchase_result_payload(idempotent_replay=True))))
    result = await confirmation_service.confirm_purchase(quote_reference=reference)

    assert len(signed_in.sent_keys) == 2
    assert signed_in.sent_keys[0] == signed_in.sent_keys[1], "a retry used a new idempotency key"
    assert result["order_created"] is True


async def test_a_timeout_is_never_re_keyed(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_purchase_error(httpx.ReadTimeout("timed out"))

    for _ in range(2):
        with pytest.raises(PurchaseOutcomeUnknownError):
            await confirmation_service.confirm_purchase(quote_reference=reference)

    assert len(set(signed_in.sent_keys)) == 1, "a timeout produced a second idempotency key"


async def test_two_quotes_for_the_same_plan_get_different_keys(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """A genuinely new purchase is a new quote, and a new quote is a new key."""
    first = await prepare(purchase_service)
    await confirmation_service.confirm_purchase(quote_reference=first)

    second = await prepare(purchase_service)
    await confirmation_service.confirm_purchase(quote_reference=second)

    assert first != second
    assert len(set(signed_in.sent_keys)) == 2


async def test_a_purchase_stops_after_the_attempt_limit(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """An unattended retry loop is stopped, and the escalation says so plainly."""
    reference = await prepare(purchase_service)
    signed_in.stub_purchase_error(httpx.ReadTimeout("timed out"))

    for _ in range(MAX_EXECUTION_ATTEMPTS):
        with pytest.raises(PurchaseOutcomeUnknownError):
            await confirmation_service.confirm_purchase(quote_reference=reference)

    with pytest.raises(PurchaseAttemptLimitError) as excinfo:
        await confirmation_service.confirm_purchase(quote_reference=reference)

    assert len(signed_in.purchase_calls) == MAX_EXECUTION_ATTEMPTS
    assert "contact esim support" in str(excinfo.value).lower()


# ------------------------------------------------------------- backend outcome mapping


@pytest.mark.parametrize(
    ("status", "title", "expected"),
    [
        (401, "BEARER_TOKEN_REQUIRED", AuthenticationRequiredError),
        (403, "FORBIDDEN", AuthenticationRequiredError),
        (404, "NOT_FOUND", PurchaseRouteUnavailableError),
        (409, "IDEMPOTENCY_KEY_CONFLICT", IdempotencyConflictError),
        (409, "IDEMPOTENT_REQUEST_IN_PROGRESS", PurchaseInProgressError),
        (422, "UNPROCESSABLE", PurchaseRejectedError),
        (424, "MCP_MANUAL_INTERVENTION_REQUIRED", PurchaseManualInterventionError),
        (429, "TOO_MANY_REQUESTS", RateLimitedError),
        (503, "MCP_PURCHASE_DISABLED", PurchaseUnavailableError),
        (400, "INSUFFICIENT_WALLET_BALANCE", InsufficientWalletBalanceError),
        (400, "BUNDLE_NOT_AVAILABLE", BundleUnavailableError),
        (400, "MCP_UNSUPPORTED_CURRENCY", PurchaseCurrencyMismatchError),
        (400, "MCP_UNSUPPORTED_PAYMENT_TYPE", NonWalletQuoteError),
    ],
)
async def test_every_documented_backend_outcome_maps_to_a_typed_error(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
    status: int,
    title: str,
    expected: type[EsimMcpError],
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_purchase(failure_response(status, title=title))

    with pytest.raises(expected):
        await confirmation_service.confirm_purchase(quote_reference=reference)


async def test_a_manual_intervention_outcome_is_never_sent_again(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """424 means the wallet may already have been charged. Nothing may be re-sent."""
    reference = await prepare(purchase_service)
    signed_in.stub_purchase(
        failure_response(
            424,
            title="MCP_MANUAL_INTERVENTION_REQUIRED",
            data=purchase_result_payload(status="MANUAL_INTERVENTION_REQUIRED", next_action="CONTACT_SUPPORT"),
        )
    )

    with pytest.raises(PurchaseManualInterventionError) as first:
        await confirmation_service.confirm_purchase(quote_reference=reference)
    with pytest.raises(PurchaseManualInterventionError) as second:
        await confirmation_service.confirm_purchase(quote_reference=reference)

    assert len(signed_in.purchase_calls) == 1, "an escalated purchase was sent again"
    message = str(first.value).lower()
    assert "do not retry" in message
    assert "contact esim support" in message
    # The escalation carries the order reference support will ask for, and nothing else.
    assert second.value.details["order_id"] == "ord-0001-aaaa-bbbb"
    assert second.value.details["retry_safe"] is False


async def test_an_in_progress_purchase_may_be_asked_about_again_with_the_same_key(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    """ "Still processing" is not a failure: the same key is how we find out what happened."""
    reference = await prepare(purchase_service)
    signed_in.stub_purchase(failure_response(409, title="IDEMPOTENT_REQUEST_IN_PROGRESS"))

    with pytest.raises(PurchaseInProgressError) as excinfo:
        await confirmation_service.confirm_purchase(quote_reference=reference)

    signed_in.stub_purchase(httpx.Response(200, json=envelope(purchase_result_payload(idempotent_replay=True))))
    result = await confirmation_service.confirm_purchase(quote_reference=reference)

    assert signed_in.sent_keys[0] == signed_in.sent_keys[1]
    assert result["order_created"] is True
    assert "do not confirm again straight away" in str(excinfo.value).lower()


async def test_a_conflicting_key_never_becomes_a_second_purchase(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_purchase(failure_response(409, title="IDEMPOTENCY_KEY_CONFLICT"))

    with pytest.raises(IdempotencyConflictError):
        await confirmation_service.confirm_purchase(quote_reference=reference)
    with pytest.raises(IdempotencyConflictError):
        await confirmation_service.confirm_purchase(quote_reference=reference)

    assert len(signed_in.purchase_calls) == 1


async def test_the_backend_being_unavailable_is_reported_as_nothing_charged(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_purchase(failure_response(503, title="MCP_PURCHASE_TEMPORARILY_UNAVAILABLE"))

    with pytest.raises(PurchaseUnavailableError) as excinfo:
        await confirmation_service.confirm_purchase(quote_reference=reference)

    message = str(excinfo.value).lower()
    assert "nothing was charged" in message
    # Nothing was spent, so the same quote may be tried again.
    signed_in.stub_purchase(httpx.Response(200, json=envelope(purchase_result_payload())))
    assert (await confirmation_service.confirm_purchase(quote_reference=reference))["order_created"] is True


@pytest.mark.parametrize("error", [httpx.ReadTimeout("timed out"), httpx.ConnectError("reset")])
async def test_a_lost_connection_is_reported_as_an_unknown_outcome(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
    error: Exception,
) -> None:
    """Never "it failed": the request may have been executed in full before the socket died."""
    reference = await prepare(purchase_service)
    signed_in.stub_purchase_error(error)

    with pytest.raises(PurchaseOutcomeUnknownError) as excinfo:
        await confirmation_service.confirm_purchase(quote_reference=reference)

    message = str(excinfo.value).lower()
    assert "never say it succeeded and never say it failed" in message
    assert "do not prepare a new quote" in message
    assert excinfo.value.details["retry_safe"] is False
    assert excinfo.value.details["new_purchase_safe"] is False


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json at all"),
        httpx.Response(200, json=envelope({"status": "SOMETHING_ELSE"})),
        httpx.Response(200, json=envelope(None)),
    ],
)
async def test_an_unreadable_success_is_an_unknown_outcome_not_a_success(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    quote_service: PurchaseQuoteService,
    session_manager: Any,
    identity_a: StubIdentityProvider,
    signed_in: Backend,
    response: httpx.Response,
) -> None:
    """A 200 this server cannot read as "completed" may still have debited the wallet."""
    reference = await prepare(purchase_service)
    signed_in.stub_purchase(response)
    owner = await owner_of(session_manager, identity_a)

    with pytest.raises(PurchaseOutcomeUnknownError):
        await confirmation_service.confirm_purchase(quote_reference=reference)

    stored = await quote_service.get_record(owner, reference)
    assert stored.status is not QuoteStatus.CONSUMED


# ------------------------------------------------------------------------ no leakage


async def test_no_secret_reaches_a_successful_result(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
) -> None:
    reference = await prepare(purchase_service)

    result = await confirmation_service.confirm_purchase(quote_reference=reference)

    blob = rendered(result).lower()
    sent_key = signed_in.sent_keys[0]
    for forbidden in (
        sent_key.lower(),
        "idempotency",
        "bearer",
        "access_token",
        "refresh_token",
        "authorization",
        "correlation",
        "developermessage",
        "c0rr3lat10n",
        "person@example.com",
    ):
        assert forbidden not in blob, f"a result leaked {forbidden!r}"


@pytest.mark.parametrize(
    "response",
    [
        failure_response(424, title="MCP_MANUAL_INTERVENTION_REQUIRED"),
        failure_response(409, title="IDEMPOTENCY_KEY_CONFLICT"),
        failure_response(400, title="INSUFFICIENT_WALLET_BALANCE"),
        failure_response(503, title="MCP_PURCHASE_DISABLED"),
    ],
)
async def test_no_secret_or_developer_message_reaches_an_error(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
    response: httpx.Response,
) -> None:
    reference = await prepare(purchase_service)
    signed_in.stub_purchase(response)

    with pytest.raises(EsimMcpError) as excinfo:
        await confirmation_service.confirm_purchase(quote_reference=reference)

    blob = f"{excinfo.value} {rendered(excinfo.value.to_dict())}".lower()
    for forbidden in (
        signed_in.sent_keys[0].lower(),
        "bearer",
        "access_token",
        "developermessage",
        "localized text",
        "traceback",
    ):
        assert forbidden not in blob, f"an error leaked {forbidden!r}"


async def test_the_raw_idempotency_key_never_reaches_a_log_record(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    signed_in: Backend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a short fingerprint may be logged: a log line is not a place for a purchase key."""
    reference = await prepare(purchase_service)

    with caplog.at_level(logging.DEBUG):
        await confirmation_service.confirm_purchase(quote_reference=reference)

    key = signed_in.sent_keys[0]
    logged = " ".join(f"{record.getMessage()} {rendered(record.__dict__)}" for record in caplog.records)
    assert key not in logged
    # The fingerprint that *is* logged must not be the key or a prefix of it.
    assert "key_fp" in logged
    assert key[:12] not in logged


async def test_the_mcp_binding_buys_a_plan_and_replays_a_repeat(settings: Any, backend: Backend) -> None:
    """Through ``MCPServer.call_tool``, the way a real client reaches it.

    The service-level tests above would not notice a tool registered with a mis-wired
    argument, a broken schema or a swallowed error channel; this one does.
    """
    from esim_mcp.server import build_components

    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    server = components.server
    try:
        await server.call_tool("request_login_otp", {"email": "person@example.com"})
        await server.call_tool("verify_login_otp", {"verification_pin": "123456", "email": "person@example.com"})
        prepared = payload_of(
            await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Wallet"})
        )
        bought = await server.call_tool("confirm_purchase", {"quote_reference": prepared["quote_id"]})
        again = await server.call_tool("confirm_purchase", {"quote_reference": prepared["quote_id"]})
    finally:
        await components.aclose()

    assert bought.is_error is not True
    first, second = payload_of(bought), payload_of(again)
    assert first["status"] == "purchased"
    assert first["charged"] is True
    assert second["order_id"] == first["order_id"]
    assert second["replayed"] is True
    assert len(backend.purchase_calls) == 1, "the binding sent a second purchase"


async def test_the_mcp_binding_surfaces_a_failure_as_a_safe_tool_error(settings: Any, backend: Backend) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    from esim_mcp.server import build_components

    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    server = components.server
    try:
        await server.call_tool("request_login_otp", {"email": "person@example.com"})
        await server.call_tool("verify_login_otp", {"verification_pin": "123456", "email": "person@example.com"})
        prepared = payload_of(
            await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Wallet"})
        )
        backend.stub_purchase(failure_response(424, title="MCP_MANUAL_INTERVENTION_REQUIRED"))

        with pytest.raises(ToolError) as excinfo:
            await server.call_tool("confirm_purchase", {"quote_reference": prepared["quote_id"]})
    finally:
        await components.aclose()

    rendered_error = str(excinfo.value).lower()
    assert "purchase_needs_support" in rendered_error
    assert "do not retry" in rendered_error
    for forbidden in ("bearer", "idempotency-key", "developermessage", "localized text"):
        assert forbidden not in rendered_error


async def test_the_mcp_binding_refuses_a_quote_the_caller_does_not_own(settings: Any, backend: Backend) -> None:
    """Two clients, one quote reference: the second must not be able to spend it."""
    from esim_mcp.purchase.store import InMemoryPurchaseQuoteStore
    from esim_mcp.server import build_components
    from esim_mcp.session.store import InMemorySessionStore

    shared_sessions = InMemorySessionStore()
    shared_quotes = InMemoryPurchaseQuoteStore()
    first = build_components(
        settings,
        store=shared_sessions,
        quote_store=shared_quotes,
        identity_provider=StubIdentityProvider("client-a"),
    )
    second = build_components(
        settings,
        store=shared_sessions,
        quote_store=shared_quotes,
        identity_provider=StubIdentityProvider("client-b"),
    )
    try:
        await first.server.call_tool("request_login_otp", {"email": "person@example.com"})
        await first.server.call_tool("verify_login_otp", {"verification_pin": "123456", "email": "person@example.com"})
        prepared = payload_of(
            await first.server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Wallet"})
        )

        await second.server.call_tool("request_login_otp", {"email": "other@example.com"})
        await second.server.call_tool("verify_login_otp", {"verification_pin": "123456", "email": "other@example.com"})

        from mcp.server.mcpserver.exceptions import ToolError

        with pytest.raises(ToolError) as excinfo:
            await second.server.call_tool("confirm_purchase", {"quote_reference": prepared["quote_id"]})
    finally:
        await first.aclose()
        await second.aclose()

    assert "quote_not_found" in str(excinfo.value).lower()
    assert not backend.purchase_calls


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


# ------------------------------------------------------- the execution store, on its own


def an_owner(session_key: str = "session-a", user_ref: str = "user-a") -> QuoteOwner:
    return QuoteOwner(session_key=session_key, user_ref=user_ref)


async def test_acquiring_twice_returns_one_key(execution_service: PurchaseExecutionService) -> None:
    owner = an_owner()

    first = await execution_service.acquire(owner, "quote-1")
    second = await execution_service.acquire(owner, "quote-1")

    assert first.idempotency_key.get_secret_value() == second.idempotency_key.get_secret_value()


async def test_an_execution_belonging_to_another_owner_is_invisible(
    execution_service: PurchaseExecutionService,
) -> None:
    """Not "refused": nobody may use this server to learn that another client holds a purchase."""
    mine = await execution_service.acquire(an_owner(), "quote-1")

    theirs = await execution_service.get(an_owner(session_key="session-b"), "quote-1")

    assert theirs is None
    # A different owner acquiring the same id gets its own record and its own key.
    other = await execution_service.acquire(an_owner(session_key="session-b"), "quote-1")
    assert other.idempotency_key.get_secret_value() != mine.idempotency_key.get_secret_value()


async def test_attempts_are_counted_before_the_call_not_after(
    execution_service: PurchaseExecutionService,
) -> None:
    """A call that dies mid-flight is still an attempt, or the limit would never bite."""
    owner = an_owner()
    execution = await execution_service.acquire(owner, "quote-1")

    counted = await execution_service.record_attempt(execution)

    assert counted.attempts == 1
    assert (await execution_service.get(owner, "quote-1")).attempts == 1


async def test_ending_a_session_drops_its_execution_records(
    service: AuthenticationService,
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    execution_service: PurchaseExecutionService,
    session_manager: Any,
    identity_a: StubIdentityProvider,
    signed_in: Backend,
) -> None:
    """An idempotency key outliving its session could let the next user replay a purchase."""
    reference = await prepare(purchase_service)
    await confirmation_service.confirm_purchase(quote_reference=reference)
    owner = await owner_of(session_manager, identity_a)
    assert await execution_service.get(owner, reference) is not None

    await service.logout()

    assert await execution_service.get(owner, reference) is None


async def test_the_stored_execution_never_holds_a_readable_key(
    purchase_service: PurchasePreparationService,
    confirmation_service: PurchaseConfirmationService,
    execution_service: PurchaseExecutionService,
    session_manager: Any,
    identity_a: StubIdentityProvider,
    signed_in: Backend,
) -> None:
    """The key is a SecretStr, so no accidental repr or dump can spill it."""
    reference = await prepare(purchase_service)
    await confirmation_service.confirm_purchase(quote_reference=reference)
    owner = await owner_of(session_manager, identity_a)

    execution = await execution_service.get(owner, reference)

    assert execution is not None
    assert execution.status is ExecutionStatus.SUCCEEDED
    assert signed_in.sent_keys[0] not in repr(execution)
    assert signed_in.sent_keys[0] not in rendered(execution.model_dump())
    assert execution.idempotency_key.get_secret_value() == signed_in.sent_keys[0]
