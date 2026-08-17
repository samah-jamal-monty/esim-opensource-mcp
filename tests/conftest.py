"""Shared fixtures. No test performs real network I/O and none requires a ``.env``."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from esim_mcp.client.account import AccountApiClient
from esim_mcp.client.auth import AuthApiClient
from esim_mcp.client.base import BackendApiClient
from esim_mcp.client.card import CardCheckoutApiClient
from esim_mcp.client.catalog import CatalogApiClient
from esim_mcp.client.consumption import ConsumptionApiClient
from esim_mcp.client.purchase import PurchaseApiClient
from esim_mcp.client.topup import TopupApiClient
from esim_mcp.client.wallet import WalletApiClient
from esim_mcp.client.wallet_topup import WalletTopupApiClient
from esim_mcp.purchase.card import CardCheckoutService, InMemoryCardCheckoutStore
from esim_mcp.purchase.execution import InMemoryPurchaseExecutionStore, PurchaseExecutionService
from esim_mcp.purchase.service import PurchaseQuoteService
from esim_mcp.purchase.store import InMemoryPurchaseQuoteStore
from esim_mcp.selection.service import EsimSelectionService
from esim_mcp.selection.store import InMemoryEsimSelectionStore
from esim_mcp.session.identity import ClientIdentity, ClientIdentityProvider
from esim_mcp.session.manager import SessionManager
from esim_mcp.session.store import InMemorySessionStore
from esim_mcp.settings import Settings
from esim_mcp.tools.account import AccountService
from esim_mcp.tools.authentication import AuthenticationService
from esim_mcp.tools.card_checkout import CardPaymentService
from esim_mcp.tools.catalog import CatalogService
from esim_mcp.tools.consumption import ConsumptionService
from esim_mcp.tools.esim_topup import EsimTopupService
from esim_mcp.tools.purchase_execution import PurchaseConfirmationService
from esim_mcp.tools.purchase_preparation import PurchasePreparationService
from esim_mcp.tools.wallet_topup import WalletTopupService
from esim_mcp.topup.service import (
    EsimTopupExecutionService,
    EsimTopupQuoteService,
    WalletTopupCheckoutService,
    WalletTopupQuoteService,
)
from esim_mcp.topup.store import (
    InMemoryEsimTopupExecutionStore,
    InMemoryEsimTopupQuoteStore,
    InMemoryWalletTopupCheckoutStore,
    InMemoryWalletTopupQuoteStore,
)

BASE_URL = "https://backend.test"
API_URL = f"{BASE_URL}/api/v1"
TEST_SALT = "unit-test-salt-value-that-is-long-enough-0123456789"


class StubIdentityProvider(ClientIdentityProvider):
    """Deterministic identity, standing in for a verified transport principal."""

    def __init__(self, value: str, source: str = "test") -> None:
        self._identity = ClientIdentity(value=value, source=source)

    async def resolve(self, ctx: Any | None = None) -> ClientIdentity:
        return self._identity

    @property
    def identity(self) -> ClientIdentity:
        return self._identity


def b64url(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_jwt(*, expires_in: int = 3600, subject: str = "user-1234-5678", signature: str = "sig") -> str:
    """A JWT-shaped token. Only ``exp`` matters; the signature is never verified."""
    header = b64url({"alg": "HS256", "typ": "JWT"})
    body = b64url({"sub": subject, "exp": int(time.time()) + expires_in})
    return f"{header}.{body}.{signature}"


def envelope(
    data: Any = None,
    *,
    status: str = "success",
    title: str | None = "Success",
    message: str | None = None,
    developer_message: str | None = None,
    response_code: int = 200,
    total_count: int = 0,
) -> dict[str, Any]:
    """Build a backend response envelope."""
    return {
        "status": status,
        "totalCount": total_count,
        "data": data,
        "title": title,
        "message": message,
        "developerMessage": developer_message,
        "responseCode": response_code,
    }


def auth_payload(
    *,
    access_token: str | None = None,
    refresh_token: str = "refresh-token-value",
    email: str | None = "person@example.com",
    msisdn: str | None = None,
    balance: float | None = 12.5,
) -> dict[str, Any]:
    """A backend ``AuthResponseDTO`` payload."""
    return {
        "access_token": access_token or make_jwt(),
        "refresh_token": refresh_token,
        "user_token": "b3f1c0de-1111-2222-3333-444455556666",
        "is_verified": True,
        "user_info": {
            "is_verified": True,
            "email": email,
            "msisdn": msisdn,
            "first_name": "Test",
            "last_name": "User",
            "currency_code": "USD",
            "balance": balance,
            "language": "en",
        },
    }


# --------------------------------------------------------------------------- catalogue
#
# Payloads mirror the backend's CountryDTO / RegionDTO / BundleDTO / HomeResponseDto
# exactly, including the placeholders it substitutes for missing upstream values
# ("Unknown", "") and the "∞ Unlimited" data display.

FRANCE_GUID = "11111111-1111-4111-8111-111111111111"
LEBANON_GUID = "22222222-2222-4222-8222-222222222222"
UAE_GUID = "33333333-3333-4333-8333-333333333333"


def country_payload(
    *,
    guid: str = FRANCE_GUID,
    country: str = "France",
    country_code: str | None = "FR",
    iso3_code: str | None = "FRA",
    alternative_country: str | None = "",
    zone_name: str | None = "Europe",
) -> dict[str, Any]:
    """A backend ``CountryDTO`` payload."""
    return {
        "id": guid,
        "country": country,
        "country_code": country_code,
        "iso3_code": iso3_code,
        "alternative_country": alternative_country,
        "zone_name": zone_name,
        "icon": "https://cdn.test/media/country/fra.png",
        "operator_list": ["Orange"],
    }


def region_payload(
    *,
    region_code: str = "EUR",
    region_name: str = "Europe",
    guid: str = "44444444-4444-4444-8444-444444444444",
) -> dict[str, Any]:
    """A backend ``RegionDTO`` payload."""
    return {
        "region_code": region_code,
        "region_name": region_name,
        "zone_name": region_name,
        "icon": f"https://cdn.test/media/region/{region_code.lower()}.png",
        "guid": guid,
    }


def bundle_payload(
    *,
    bundle_code: str = "aaaaaaaa-0000-4000-8000-000000000001",
    name: str = "France 5GB / 30 Days",
    gprs_limit: float = 5.0,
    data_unit: str = "GB",
    unlimited: bool = False,
    price: float = 12.5,
    currency_code: str = "USD",
    validity: int = 30,
    validity_label: str = "Day",
    countries: list[dict[str, Any]] | None = None,
    supported_ships: list[dict[str, Any]] | None = None,
    category_type: str = "COUNTRY",
    is_active: bool = True,
) -> dict[str, Any]:
    """A backend ``BundleDTO`` payload."""
    display = "∞ Unlimited" if unlimited else f"{gprs_limit} {data_unit}"
    return {
        "display_title": name,
        "display_subtitle": f"Stay connected in France with {display}",
        "bundle_code": bundle_code,
        "bundle_category": {"type": category_type, "title": category_type.title(), "code": category_type},
        "bundle_region": [],
        "bundle_marketing_name": name,
        "bundle_name": name,
        "count_countries": len(countries) if countries is not None else 1,
        "currency_code": currency_code,
        "gprs_limit": -1 if unlimited else gprs_limit,
        "gprs_limit_display": display,
        "original_price": price,
        "price": price,
        "price_display": f"{price:.2f} {currency_code}",
        "unlimited": unlimited,
        "validity": validity,
        "validity_label": validity_label,
        "validity_display": f"{validity} {validity_label}",
        "plan_type": "Data only",
        "activity_policy": "The validity period starts when the eSIM connects to any supported networks.",
        "countries": countries if countries is not None else [country_payload()],
        "supported_ships": supported_ships or [],
        "icon": "https://cdn.test/media/bundle.png",
        "label": None,
        "is_stockable": True,
        "bundle_info_code": "BND-1",
        "is_active": is_active,
    }


def home_payload(
    *,
    countries: list[dict[str, Any]] | None = None,
    regions: list[dict[str, Any]] | None = None,
    cruise_bundles: list[dict[str, Any]] | None = None,
    global_bundles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A backend ``HomeResponseDto`` payload."""
    return {
        "countries": countries if countries is not None else [country_payload()],
        "regions": regions if regions is not None else [region_payload()],
        "cruise_bundles": cruise_bundles if cruise_bundles is not None else [],
        "global_bundles": global_bundles if global_bundles is not None else [],
    }


CATALOG_COUNTRIES: list[dict[str, Any]] = [
    country_payload(),
    country_payload(guid=LEBANON_GUID, country="Lebanon", country_code="LB", iso3_code="LBN", zone_name="Middle East"),
    country_payload(
        guid=UAE_GUID,
        country="United Arab Emirates",
        country_code="AE",
        iso3_code="ARE",
        alternative_country="UAE",
        zone_name="Middle East",
    ),
    country_payload(
        guid="55555555-5555-4555-8555-555555555555",
        country="French Guiana",
        country_code="GF",
        iso3_code="GUF",
        zone_name="South America",
    ),
]

CATALOG_REGIONS: list[dict[str, Any]] = [
    region_payload(),
    region_payload(region_code="MEA", region_name="Middle East", guid="66666666-6666-4666-8666-666666666666"),
]


def wallet_payload(*, balance: float | str = 20.0, currency: str = "USD") -> dict[str, Any]:
    """A backend ``UserWalletResponse`` payload.

    The real backend declares ``balance: float``, so a fixture sends a float too -- which is
    exactly the value the purchase layer has to turn into an exact :class:`~decimal.Decimal`.
    """
    return {"balance": balance, "currency": currency}


@pytest.fixture
def settings() -> Settings:
    return Settings.build(
        api_base_url=BASE_URL,
        environment="qa",
        device_id_salt=TEST_SALT,
        default_locale="en",
        default_currency="USD",
        token_refresh_window_seconds=60,
        login_challenge_ttl_seconds=300,
        purchase_quote_ttl_seconds=300,
        max_active_quotes_per_user=5,
    )


@pytest.fixture
def store() -> InMemorySessionStore:
    return InMemorySessionStore()


@pytest.fixture
async def backend_client(settings: Settings) -> AsyncIterator[BackendApiClient]:
    client = BackendApiClient(settings)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def auth_client(backend_client: BackendApiClient) -> AuthApiClient:
    return AuthApiClient(backend_client)


@pytest.fixture
def catalog_client(backend_client: BackendApiClient) -> CatalogApiClient:
    return CatalogApiClient(backend_client)


@pytest.fixture
def catalog_service(
    settings: Settings,
    catalog_client: CatalogApiClient,
    identity_a: StubIdentityProvider,
) -> CatalogService:
    """A catalogue service for one caller. Deliberately built with no session at all."""
    return CatalogService(settings, catalog_client, identity_a)


@pytest.fixture
def session_manager(settings: Settings, store: InMemorySessionStore, auth_client: AuthApiClient) -> SessionManager:
    return SessionManager(settings, store, auth_client)


@pytest.fixture
def identity_a() -> StubIdentityProvider:
    return StubIdentityProvider("client-a")


@pytest.fixture
def identity_b() -> StubIdentityProvider:
    return StubIdentityProvider("client-b")


@pytest.fixture
def make_service(
    settings: Settings,
    auth_client: AuthApiClient,
    session_manager: SessionManager,
) -> Callable[[ClientIdentityProvider], AuthenticationService]:
    """Build services for different callers that share one store and one HTTP client."""

    def factory(identity_provider: ClientIdentityProvider) -> AuthenticationService:
        return AuthenticationService(settings, auth_client, session_manager, identity_provider)

    return factory


@pytest.fixture
def service(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    identity_a: StubIdentityProvider,
) -> AuthenticationService:
    return make_service(identity_a)


# ------------------------------------------------------------------ purchase preparation


@pytest.fixture
def wallet_client(backend_client: BackendApiClient) -> WalletApiClient:
    return WalletApiClient(backend_client)


@pytest.fixture
def quote_store() -> InMemoryPurchaseQuoteStore:
    return InMemoryPurchaseQuoteStore()


@pytest.fixture
def quote_service(settings: Settings, quote_store: InMemoryPurchaseQuoteStore) -> PurchaseQuoteService:
    return PurchaseQuoteService(settings, quote_store)


@pytest.fixture
def make_purchase_service(
    settings: Settings,
    catalog_client: CatalogApiClient,
    wallet_client: WalletApiClient,
    session_manager: SessionManager,
    quote_service: PurchaseQuoteService,
) -> Callable[[ClientIdentityProvider], PurchasePreparationService]:
    """Build preparation services for different callers sharing one store and one quote store.

    Sharing is the point: quote isolation between two callers is only meaningful when both
    are looking at the same underlying store.
    """
    session_manager.add_invalidation_listener(quote_service.invalidate_session)

    def factory(identity_provider: ClientIdentityProvider) -> PurchasePreparationService:
        return PurchasePreparationService(
            settings,
            catalog_client,
            wallet_client,
            session_manager,
            identity_provider,
            quote_service,
        )

    return factory


@pytest.fixture
def purchase_service(
    make_purchase_service: Callable[[ClientIdentityProvider], PurchasePreparationService],
    identity_a: StubIdentityProvider,
) -> PurchasePreparationService:
    return make_purchase_service(identity_a)


# -------------------------------------------------------------------- purchase execution


def purchase_result_payload(
    *,
    status: str = "COMPLETED",
    order_id: str | None = "ord-0001-aaaa-bbbb",
    payment_status: str = "COMPLETED",
    order_status: str = "SUCCESS",
    provisioning_status: str = "COMPLETED",
    next_action: str = "GET_ESIM_BY_ORDER",
    idempotent_replay: bool = False,
    quote_reference: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """A backend ``McpPurchaseResponse`` payload, field for field.

    Mirrors the platform's dedicated MCP purchase contract, which deliberately carries no
    token, raw idempotency key, wallet transaction internal, activation code or ICCID.
    """
    return {
        "status": status,
        "order_id": order_id,
        "payment_method": "Wallet",
        "payment_status": payment_status,
        "order_status": order_status,
        "idempotent_replay": idempotent_replay,
        "provisioning_status": provisioning_status,
        "next_action": next_action,
        "quote_reference": quote_reference,
        "correlation_id": "c0rr3lat10n",
        "message": message,
    }


@pytest.fixture
def purchase_client(backend_client: BackendApiClient) -> PurchaseApiClient:
    return PurchaseApiClient(backend_client)


@pytest.fixture
def execution_store() -> InMemoryPurchaseExecutionStore:
    return InMemoryPurchaseExecutionStore()


@pytest.fixture
def execution_service(execution_store: InMemoryPurchaseExecutionStore) -> PurchaseExecutionService:
    return PurchaseExecutionService(execution_store)


@pytest.fixture
def make_confirmation_service(
    settings: Settings,
    purchase_client: PurchaseApiClient,
    session_manager: SessionManager,
    quote_service: PurchaseQuoteService,
    execution_service: PurchaseExecutionService,
) -> Callable[[ClientIdentityProvider], PurchaseConfirmationService]:
    """Build confirmation services for different callers over one shared set of stores.

    Wired exactly as ``build_components`` wires the real server, including the invalidation
    listener: an execution record (and the idempotency key in it) must not outlive its
    session any more than a quote does.
    """
    session_manager.add_invalidation_listener(execution_service.invalidate_session)

    def factory(identity_provider: ClientIdentityProvider) -> PurchaseConfirmationService:
        return PurchaseConfirmationService(
            settings,
            purchase_client,
            session_manager,
            identity_provider,
            quote_service,
            execution_service,
        )

    return factory


@pytest.fixture
def confirmation_service(
    make_confirmation_service: Callable[[ClientIdentityProvider], PurchaseConfirmationService],
    identity_a: StubIdentityProvider,
) -> PurchaseConfirmationService:
    return make_confirmation_service(identity_a)


# ------------------------------------------------------------------------- card checkout
#
# The hosted payment page lives on a *different* host from the backend on purpose: that is
# how the real thing works, and a fixture that served the link from the backend host would
# quietly hide the fact that this server hands a user a third-party address.

CHECKOUT_URL = "https://checkout.test/pay/session-reference-0001"
PAYMENT_REFERENCE = "pay-ref-0001"
ORDER_ID = "ord-0001-aaaa-bbbb"


def card_checkout_payload(
    *,
    payment_reference: str | None = PAYMENT_REFERENCE,
    order_id: str | None = ORDER_ID,
    checkout_url: str | None = CHECKOUT_URL,
    status: str = "PENDING",
    amount: float | str | None = "8.06",
    currency: str = "USD",
    expires_at: str | None = "2026-01-01T00:30:00+00:00",
    idempotent_replay: bool = False,
    message: str | None = None,
) -> dict[str, Any]:
    """The backend's documented create-checkout payload, field for field.

    Extended with the provider fields a real hosted-checkout response would carry -- a session
    id and a client secret -- and with a ``developerMessage``, so the "closed field set"
    property is tested against a payload that actually contains something worth leaking rather
    than against a sanitized one. None of the extras is a field this server names, so all of
    them must vanish at parse time.
    """
    return {
        "payment_reference": payment_reference,
        "order_id": order_id,
        "checkout_url": checkout_url,
        "status": status,
        "amount": amount,
        "currency": currency,
        "expires_at": expires_at,
        "idempotent_replay": idempotent_replay,
        "correlation_id": "c0rr3lat10n",
        "message": message,
        # Not part of the contract. Present so the parser is proved to drop them.
        "provider_session_id": "sess_secret_value_0001",
        "provider_client_secret": "secret_do_not_leak_0001",
        "developerMessage": "internal backend detail 0001",
    }


def card_status_payload(
    *,
    status: str = "PENDING",
    payment_reference: str = PAYMENT_REFERENCE,
    order_id: str | None = ORDER_ID,
    amount: float | str | None = "8.06",
    currency: str = "USD",
    bundle_code: str | None = "aaaaaaaa-0000-4000-8000-000000000001",
    quote_reference: str | None = None,
    expires_at: str | None = "2026-01-01T00:30:00+00:00",
    provisioned: bool = False,
    next_action: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """The backend's documented card-payment-status payload, field for field.

    Carries the same non-contract extras as the checkout payload, for the same reason.
    """
    return {
        "payment_reference": payment_reference,
        "status": status,
        "order_id": order_id,
        "amount": amount,
        "currency": currency,
        "bundle_code": bundle_code,
        "quote_reference": quote_reference,
        "expires_at": expires_at,
        "provisioned": provisioned,
        "next_action": next_action,
        "correlation_id": "c0rr3lat10n",
        "message": message,
        # Not part of the contract. Present so the parser is proved to drop them.
        "provider_session_id": "sess_secret_value_0001",
        "developerMessage": "internal backend detail 0001",
    }


@pytest.fixture
def card_client(backend_client: BackendApiClient) -> CardCheckoutApiClient:
    return CardCheckoutApiClient(backend_client)


@pytest.fixture
def checkout_store() -> InMemoryCardCheckoutStore:
    return InMemoryCardCheckoutStore()


@pytest.fixture
def checkout_service(checkout_store: InMemoryCardCheckoutStore) -> CardCheckoutService:
    return CardCheckoutService(checkout_store)


@pytest.fixture
def make_card_service(
    settings: Settings,
    card_client: CardCheckoutApiClient,
    session_manager: SessionManager,
    quote_service: PurchaseQuoteService,
    checkout_service: CardCheckoutService,
) -> Callable[[ClientIdentityProvider], CardPaymentService]:
    """Build card services for different callers over one shared set of stores.

    Wired exactly as ``build_components`` wires the real server, including the invalidation
    listener: a checkout record (and the idempotency key in it) must not outlive its session
    any more than a quote does.
    """
    session_manager.add_invalidation_listener(checkout_service.invalidate_session)

    def factory(identity_provider: ClientIdentityProvider) -> CardPaymentService:
        return CardPaymentService(
            settings,
            card_client,
            session_manager,
            identity_provider,
            quote_service,
            checkout_service,
        )

    return factory


@pytest.fixture
def card_service(
    make_card_service: Callable[[ClientIdentityProvider], CardPaymentService],
    identity_a: StubIdentityProvider,
) -> CardPaymentService:
    return make_card_service(identity_a)


# --------------------------------------------------------------- read-only account history


@pytest.fixture
def account_client(backend_client: BackendApiClient) -> AccountApiClient:
    return AccountApiClient(backend_client)


@pytest.fixture
def esim_selection_store() -> InMemoryEsimSelectionStore:
    return InMemoryEsimSelectionStore()


@pytest.fixture
def esim_selection_service(
    session_manager: SessionManager, esim_selection_store: InMemoryEsimSelectionStore
) -> EsimSelectionService:
    """One selection service for the whole graph, wired exactly as build_components wires it.

    Shared rather than per-caller on purpose: a number recorded by get_my_esims has to be the
    same number get_esim_consumption resolves, and the isolation assertions are only
    meaningful when both callers look at one store. The invalidation listener is registered
    here too, so a logout drops the listing in tests exactly as it does in the real server.
    """
    service = EsimSelectionService(esim_selection_store)
    session_manager.add_invalidation_listener(service.invalidate_session)
    return service


@pytest.fixture
def make_account_service(
    settings: Settings,
    account_client: AccountApiClient,
    session_manager: SessionManager,
    esim_selection_service: EsimSelectionService,
) -> Callable[[ClientIdentityProvider], AccountService]:
    """Build account services for different callers over one shared session manager.

    Sharing the session manager is the point: "one client cannot read another's eSIMs" is
    only a meaningful assertion when both callers are looking at the same session store.
    """

    def factory(identity_provider: ClientIdentityProvider) -> AccountService:
        return AccountService(
            settings, account_client, session_manager, identity_provider, esim_selection_service
        )

    return factory


@pytest.fixture
def account_service(
    make_account_service: Callable[[ClientIdentityProvider], AccountService],
    identity_a: StubIdentityProvider,
) -> AccountService:
    return make_account_service(identity_a)


# ------------------------------------------------------------------ purchased eSIM reads
#
# Payloads mirror the backend's own contracts field for field: ``EsimBundleResponse`` from
# ``GET /user/my-esim`` and ``ConsumptionResponse`` from ``GET /user/consumption/{iccid}``,
# including the epoch-seconds date strings the backend's field validators produce.

#: Two eSIMs on one account, so "which one did you mean" is testable rather than inferred.
ICCID_A = "8931080019123456789"
ICCID_B = "8931080019987654321"

#: A third belonging to a *different* account. It must never be reachable from account A.
ICCID_FOREIGN = "8931080019555555555"


def esim_payload(
    *,
    iccid: str = ICCID_A,
    bundle_code: str = "aaaaaaaa-0000-4000-8000-000000000001",
    label: str | None = "Paris trip",
    plan_started: bool = True,
    bundle_expired: bool = False,
    is_topup_allowed: bool = True,
    name: str = "France 5GB / 30 Days",
) -> dict[str, Any]:
    """A backend ``EsimBundleResponse`` payload, trimmed to the fields this server reads."""
    return {
        "is_topup_allowed": is_topup_allowed,
        "plan_started": plan_started,
        "bundle_expired": bundle_expired,
        "label_name": label,
        "order_number": "ord-0001-aaaa-bbbb",
        "order_status": "success",
        "qr_code_value": "LPA:1$smdp.test$ACTIVATION-CODE",
        "activation_code": "ACTIVATION-CODE",
        "smdp_address": "smdp.test",
        "validity_date": "1786000000",
        "iccid": iccid,
        "payment_date": "1754472554",
        "display_title": name,
        "display_subtitle": f"Stay connected with {name}",
        "bundle_code": bundle_code,
        "bundle_marketing_name": name,
        "bundle_name": name,
        "count_countries": 1,
        "currency_code": "USD",
        "gprs_limit_display": "5.0 GB",
        "price": 12.5,
        "price_display": "12.50 USD",
        "unlimited": False,
        "validity": 30,
        "validity_label": "Day",
        "validity_display": "30 Day",
        "plan_type": "Data only",
        "activity_policy": "The validity period starts when the eSIM connects to any supported networks.",
        "bundle_message": [],
        "countries": [country_payload()],
        "supported_ships": [],
        "icon": "https://cdn.test/media/bundle.png",
        "transaction_history": [],
    }


def consumption_payload(
    *,
    data_allocated: float | str | None = 5.0,
    data_used: float | str | None = 1.25,
    data_remaining: float | str | None = 3.75,
    unit: str = "GB",
    plan_status: str = "Active",
    expiry_date: str = "2026-09-10T12:00:00+00:00",
) -> dict[str, Any]:
    """A backend ``ConsumptionResponse`` payload, built exactly as ``DtoMapper`` builds it.

    Extended with a ``developerMessage`` and an upstream-looking extra so the "closed field
    set" property is tested against a payload that contains something worth dropping.
    """
    return {
        "data_allocated": data_allocated,
        "data_used": data_used,
        "data_remaining": data_remaining,
        "data_allocated_display": f"{data_allocated} {unit}",
        "data_used_display": f"{data_used} {unit}",
        "data_remaining_display": f"{data_remaining} {unit}",
        "plan_status": plan_status,
        "expiry_date": expiry_date,
        # Not part of the contract. Present so the parser is proved to drop them.
        "provider_subscriber_id": "sub_secret_0001",
        "developerMessage": "internal backend detail 0001",
    }


def topup_bundle_payload(
    *,
    bundle_code: str = "tttttttt-0000-4000-8000-000000000001",
    name: str = "France Top-up 3GB / 15 Days",
    price: float = 6.5,
    gprs_limit: float = 3.0,
    validity: int = 15,
    is_active: bool = True,
) -> dict[str, Any]:
    """One compatible top-up, as ``GET /user/related-topup/...`` returns it (a ``BundleDTO``)."""
    return bundle_payload(
        bundle_code=bundle_code,
        name=name,
        price=price,
        gprs_limit=gprs_limit,
        validity=validity,
        validity_label="Day",
        is_active=is_active,
    )


@pytest.fixture
def consumption_client(backend_client: BackendApiClient) -> ConsumptionApiClient:
    return ConsumptionApiClient(backend_client)


@pytest.fixture
def make_consumption_service(
    settings: Settings,
    account_client: AccountApiClient,
    consumption_client: ConsumptionApiClient,
    session_manager: SessionManager,
    esim_selection_service: EsimSelectionService,
) -> Callable[[ClientIdentityProvider], ConsumptionService]:
    """Build consumption services for different callers over one shared session manager.

    Sharing the session manager is the point: "one client cannot read another's usage" is
    only a meaningful assertion when both callers look at the same session store.
    """

    def factory(identity_provider: ClientIdentityProvider) -> ConsumptionService:
        return ConsumptionService(
            settings,
            account_client,
            consumption_client,
            session_manager,
            identity_provider,
            esim_selection_service,
        )

    return factory


@pytest.fixture
def consumption_service(
    make_consumption_service: Callable[[ClientIdentityProvider], ConsumptionService],
    identity_a: StubIdentityProvider,
) -> ConsumptionService:
    return make_consumption_service(identity_a)


# ----------------------------------------------------------------------------- eSIM top-up


@pytest.fixture
def topup_client(backend_client: BackendApiClient) -> TopupApiClient:
    return TopupApiClient(backend_client)


@pytest.fixture
def esim_topup_quote_store() -> InMemoryEsimTopupQuoteStore:
    return InMemoryEsimTopupQuoteStore()


@pytest.fixture
def esim_topup_quote_service(
    settings: Settings, esim_topup_quote_store: InMemoryEsimTopupQuoteStore
) -> EsimTopupQuoteService:
    return EsimTopupQuoteService(settings, esim_topup_quote_store)


@pytest.fixture
def esim_topup_execution_store() -> InMemoryEsimTopupExecutionStore:
    return InMemoryEsimTopupExecutionStore()


@pytest.fixture
def esim_topup_execution_service(
    esim_topup_execution_store: InMemoryEsimTopupExecutionStore,
) -> EsimTopupExecutionService:
    return EsimTopupExecutionService(esim_topup_execution_store)


@pytest.fixture
async def qa_backend_client(qa_topup_settings: Settings) -> AsyncIterator[BackendApiClient]:
    """A transport built from QA settings, so its route guard admits the QA top-up path.

    Deliberately a second client rather than a mutated one: in the real server the transport
    and the service read the *same* Settings object, so a test that flipped only the service
    would be testing a configuration that cannot exist.
    """
    client = BackendApiClient(qa_topup_settings)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def qa_topup_client(qa_backend_client: BackendApiClient) -> TopupApiClient:
    return TopupApiClient(qa_backend_client)


@pytest.fixture
def qa_topup_settings() -> Settings:
    """Settings with the QA eSIM-top-up execution flag ON.

    A separate fixture rather than a mutated ``settings``, so the default everywhere else
    stays off and any test that reaches execution has to say so out loud.
    """
    return Settings.build(
        api_base_url=BASE_URL,
        environment="qa",
        device_id_salt=TEST_SALT,
        default_locale="en",
        default_currency="USD",
        token_refresh_window_seconds=60,
        login_challenge_ttl_seconds=300,
        purchase_quote_ttl_seconds=300,
        max_active_quotes_per_user=5,
        esim_topup_execution_enabled=True,
    )


@pytest.fixture
def make_esim_topup_service(
    settings: Settings,
    account_client: AccountApiClient,
    topup_client: TopupApiClient,
    qa_topup_client: TopupApiClient,
    wallet_client: WalletApiClient,
    session_manager: SessionManager,
    esim_topup_quote_service: EsimTopupQuoteService,
    esim_topup_execution_service: EsimTopupExecutionService,
    esim_selection_service: EsimSelectionService,
) -> Callable[..., EsimTopupService]:
    """Build top-up services for different callers over one shared set of stores.

    Wired exactly as ``build_components`` wires the real server, including both invalidation
    listeners: a quote that *names an ICCID*, and the one-attempt lock that stands in for
    idempotency, must neither outlive the session that made them.

    ``execution_settings`` overrides the settings for callers that need the QA execution flag
    on. Everything else gets the default, which is off.
    """
    session_manager.add_invalidation_listener(esim_topup_quote_service.invalidate_session)
    session_manager.add_invalidation_listener(esim_topup_execution_service.invalidate_session)

    def factory(
        identity_provider: ClientIdentityProvider, execution_settings: Settings | None = None
    ) -> EsimTopupService:
        # The transport is chosen alongside the settings, exactly as build_components does:
        # one Settings object governs both the service gate and the route guard.
        return EsimTopupService(
            execution_settings or settings,
            account_client,
            qa_topup_client if execution_settings is not None else topup_client,
            wallet_client,
            session_manager,
            identity_provider,
            esim_topup_quote_service,
            esim_topup_execution_service,
            esim_selection_service,
        )

    return factory


@pytest.fixture
def esim_topup_service(
    make_esim_topup_service: Callable[..., EsimTopupService],
    identity_a: StubIdentityProvider,
) -> EsimTopupService:
    """The default: execution flag OFF, so nothing here can charge anybody."""
    return make_esim_topup_service(identity_a)


@pytest.fixture
def qa_esim_topup_service(
    make_esim_topup_service: Callable[..., EsimTopupService],
    identity_a: StubIdentityProvider,
    qa_topup_settings: Settings,
) -> EsimTopupService:
    """The QA build: execution flag ON. Every test using this one is exercising a write."""
    return make_esim_topup_service(identity_a, qa_topup_settings)


# --------------------------------------------------------------------------- wallet top-up
#
# The hosted page lives on a *different* host from the backend on purpose: that is how the
# real thing works, and a fixture that served the link from the backend host would quietly
# hide the fact that this server hands a user a third-party address.

TOPUP_CHECKOUT_URL = "https://checkout.test/pay/topup-session-0001"
TOPUP_PAYMENT_REFERENCE = "topup-ref-0001"


def topup_options_payload(
    *,
    currency: str = "USD",
    minimum_amount_exclusive: str = "0.50",
    maximum_amount: str | None = "100.00",
    current_balance: str | None = "12.50",
    can_topup: bool = True,
    message: str | None = None,
) -> dict[str, Any]:
    """The backend's documented top-up options payload, field for field."""
    return {
        "currency": currency,
        "minimum_amount_exclusive": minimum_amount_exclusive,
        "maximum_amount": maximum_amount,
        "current_balance": current_balance,
        "limits": [
            {"code": "minimum_amount", "value": 0.5, "remaining": None, "unit": "amount"},
            {"code": "daily_amount", "value": 100.0, "remaining": 100.0, "unit": "amount"},
            {"code": "daily_count", "value": 2.0, "remaining": 2.0, "unit": "count"},
        ],
        "can_topup": can_topup,
        "fees": [],
        "message": message,
    }


def wallet_topup_checkout_payload(
    *,
    payment_reference: str | None = TOPUP_PAYMENT_REFERENCE,
    order_id: str | None = "ord-topup-0001",
    checkout_url: str | None = TOPUP_CHECKOUT_URL,
    status: str = "PENDING",
    amount: str | float | None = "25.00",
    currency: str = "USD",
    expires_at: str | None = "2026-01-01T00:31:00+00:00",
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    """The backend's documented top-up checkout payload, with non-contract extras.

    The extras are the point: a hosted-checkout payload is exactly the shape of thing that
    carries a provider session id and a client secret, so the parser is proved to drop them.
    """
    return {
        "payment_reference": payment_reference,
        "order_id": order_id,
        "checkout_url": checkout_url,
        "status": status,
        "amount": amount,
        "currency": currency,
        "expires_at": expires_at,
        "next_action": "OPEN_CHECKOUT_URL",
        "idempotent_replay": idempotent_replay,
        "correlation_id": "c0rr3lat10n",
        "message": None,
        "provider_session_id": "sess_secret_value_0001",
        "provider_client_secret": "secret_do_not_leak_0001",
        "developerMessage": "internal backend detail 0001",
    }


def wallet_topup_status_payload(
    *,
    status: str = "PENDING",
    payment_reference: str = TOPUP_PAYMENT_REFERENCE,
    order_id: str | None = "ord-topup-0001",
    amount: str | float | None = "25.00",
    currency: str = "USD",
    expires_at: str | None = "2026-01-01T00:31:00+00:00",
    paid: bool = False,
) -> dict[str, Any]:
    """The backend's documented top-up status payload, with the same non-contract extras."""
    return {
        "payment_reference": payment_reference,
        "status": status,
        "order_id": order_id,
        "amount": amount,
        "currency": currency,
        "expires_at": expires_at,
        "paid": paid,
        "next_action": None,
        "correlation_id": "c0rr3lat10n",
        "message": None,
        "provider_session_id": "sess_secret_value_0001",
        "developerMessage": "internal backend detail 0001",
    }


@pytest.fixture
def wallet_topup_client(backend_client: BackendApiClient) -> WalletTopupApiClient:
    return WalletTopupApiClient(backend_client)


@pytest.fixture
def wallet_topup_quote_store() -> InMemoryWalletTopupQuoteStore:
    return InMemoryWalletTopupQuoteStore()


@pytest.fixture
def wallet_topup_checkout_store() -> InMemoryWalletTopupCheckoutStore:
    return InMemoryWalletTopupCheckoutStore()


@pytest.fixture
def wallet_topup_quote_service(
    settings: Settings, wallet_topup_quote_store: InMemoryWalletTopupQuoteStore
) -> WalletTopupQuoteService:
    return WalletTopupQuoteService(settings, wallet_topup_quote_store)


@pytest.fixture
def wallet_topup_checkout_service(
    wallet_topup_checkout_store: InMemoryWalletTopupCheckoutStore,
) -> WalletTopupCheckoutService:
    return WalletTopupCheckoutService(wallet_topup_checkout_store)


@pytest.fixture
def make_wallet_topup_service(
    settings: Settings,
    wallet_topup_client: WalletTopupApiClient,
    session_manager: SessionManager,
    wallet_topup_quote_service: WalletTopupQuoteService,
    wallet_topup_checkout_service: WalletTopupCheckoutService,
) -> Callable[[ClientIdentityProvider], WalletTopupService]:
    """Build wallet top-up services for different callers over one shared set of stores.

    Wired exactly as ``build_components`` wires the real server, including both invalidation
    listeners.
    """
    session_manager.add_invalidation_listener(wallet_topup_quote_service.invalidate_session)
    session_manager.add_invalidation_listener(wallet_topup_checkout_service.invalidate_session)

    def factory(identity_provider: ClientIdentityProvider) -> WalletTopupService:
        return WalletTopupService(
            settings,
            wallet_topup_client,
            session_manager,
            identity_provider,
            wallet_topup_quote_service,
            wallet_topup_checkout_service,
        )

    return factory


@pytest.fixture
def wallet_topup_service(
    make_wallet_topup_service: Callable[[ClientIdentityProvider], WalletTopupService],
    identity_a: StubIdentityProvider,
) -> WalletTopupService:
    return make_wallet_topup_service(identity_a)


def mock_login_routes(respx_mock: Any, *, email: str = "person@example.com") -> None:
    """Wire the routes a login needs, so a test can reach an authenticated state."""
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=make_jwt(), email=email)))
    )


async def sign_in(auth_service: AuthenticationService, *, email: str = "person@example.com") -> None:
    """Drive a real OTP login through the service, against already-mocked routes."""
    await auth_service.request_login_otp(email=email)
    await auth_service.verify_login_otp(verification_pin="123456", email=email)
