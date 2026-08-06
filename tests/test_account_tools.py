"""``get_my_esims`` and ``get_order_history`` against a mocked backend.

No test here reaches QA, none buys anything and none provisions an eSIM. Only the two
read-only account routes are mocked; a call to any *other* route surfaces as an unmocked
request rather than passing silently, which is what keeps "this suite cannot spend money" a
property of the harness rather than a promise in a comment.

The properties asserted are the ones that would matter if they regressed:

* both tools need a signed-in user, and neither takes an identifier that could name another;
* one MCP client can never read another client's eSIMs or orders;
* an account with nothing is reported as having nothing, never as an error;
* the backend's own paging is passed through under its own parameter names;
* install credentials are returned to the owner and never written to a log;
* a receipt's ``receipt_email``, billing address and provider charge id never reach a result;
* no consumption figure is reported, because the backend sends none.
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

from esim_mcp.client.account import DEFAULT_PAGE_INDEX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from esim_mcp.errors import (
    AuthenticationRequiredError,
    BackendUnavailableError,
    InvalidInputError,
    RateLimitedError,
)
from esim_mcp.session.identity import ClientIdentityProvider
from esim_mcp.tools.account import AccountService
from esim_mcp.tools.authentication import AuthenticationService
from tests.conftest import (
    API_URL,
    StubIdentityProvider,
    envelope,
    mock_login_routes,
    sign_in,
)

MY_ESIM_URL = f"{API_URL}/user/my-esim"
ORDER_HISTORY_URL = f"{API_URL}/user/order-history"

ICCID = "89000000000000000001"
ACTIVATION_CODE = "0XB06-QMWMJ-OYAK1-M4NLV"
SMDP_ADDRESS = "smdp.test"
QR_VALUE = f"LPA:1${SMDP_ADDRESS}${ACTIVATION_CODE}"

#: Values a fixture payload carries that must never reach a result or a log.
LEAKABLE = ("receipt@example.com", "12 Billing Street", "ch_secret_charge_id")


def rendered(value: Any) -> str:
    return json.dumps(value, default=str)


def esim_payload(**overrides: Any) -> dict[str, Any]:
    """The backend's ``EsimBundleResponse``, field for field.

    Extended with the values a real receipt would carry that this server must drop, so the
    "closed field set" property is tested against a payload that actually contains something
    worth leaking. Dates are epoch-seconds strings, exactly as the backend's own
    ``convert_to_timestamp`` validator produces them.
    """
    payload = {
        "is_topup_allowed": True,
        "plan_started": True,
        "bundle_expired": False,
        "label_name": "Trip to France",
        "order_number": "ord-0001",
        "order_status": "SUCCESS",
        "searched_countries": {"countries": [{"iso3_code": "FRA", "country_name": "France"}]},
        "qr_code_value": QR_VALUE,
        "activation_code": ACTIVATION_CODE,
        "smdp_address": SMDP_ADDRESS,
        "validity_date": "1785500000",
        "iccid": ICCID,
        "payment_date": "1754472554",
        "shared_with": None,
        "display_title": "France 5GB",
        "display_subtitle": "30 days",
        "bundle_code": "bundle-0001",
        "bundle_category": {"name": "COUNTRY"},
        "bundle_marketing_name": "France 5GB / 30 Days",
        "bundle_name": "France5GB30days",
        "count_countries": 1,
        "currency_code": "USD",
        "gprs_limit_display": "5 GB",
        "price": 8.06,
        "price_display": "8.06 USD",
        "unlimited": False,
        "validity": 30,
        "validity_label": "30 days",
        "validity_display": "30 days",
        "plan_type": "Data only",
        "activity_policy": "Starts on first connection",
        "bundle_message": [],
        "countries": [],
        "supported_ships": [],
        "icon": None,
        "transaction_history": [
            {
                "user_order_id": "ord-0001",
                "iccid": ICCID,
                "bundle_type": "Primary Bundle",
                "plan_started": True,
                "bundle_expired": False,
                "created_at": "1754472554",
                "bundle": {"bundle_name": "France5GB30days", "is_stockable": True},
            }
        ],
    }
    payload.update(overrides)
    return payload


def order_payload(**overrides: Any) -> dict[str, Any]:
    """The backend's ``UserOrderHistoryResponse``, field for field, including the leakables."""
    payload = {
        "order_number": "ord-0001",
        "order_status": "SUCCESS",
        "order_amount": 8.06,
        "order_currency": "USD",
        "order_display_price": "8.06 USD",
        "order_date": "1754472554",
        "order_type": "Assign",
        "quantity": 1,
        "company_name": "Reseller Ltd",
        "company_address": "1 Reseller Way",
        "company_phone": "+10000000000",
        "company_email": "billing@example.com",
        "company_website": "https://backend.test/reseller",
        "payment_details": {
            "id": "ch_secret_charge_id",
            "description": "Bundle order",
            "payment_method": "card",
            "card_number": "4242",
            "receipt_email": "receipt@example.com",
            "address": "12 Billing Street",
            "display_brand": "visa",
            "country": "US",
            "card_display": "visa ****4242",
        },
        "payment_type": "Card",
        "bundle_details": {
            "bundle_code": "bundle-0001",
            "bundle_name": "France5GB30days",
            "bundle_marketing_name": "France 5GB / 30 Days",
            "display_title": "France 5GB",
            "display_subtitle": "30 days",
            "gprs_limit_display": "5 GB",
            "unlimited": False,
            "validity_display": "30 days",
            "plan_type": "Data only",
            "count_countries": 1,
            "price_display": "8.06 USD",
            "currency_code": "USD",
            "is_stockable": True,
            "bundle_info_code": "internal-info-code",
        },
    }
    payload.update(overrides)
    return payload


def failure_response(status: int, *, title: str) -> httpx.Response:
    return httpx.Response(
        status,
        json=envelope(
            None,
            status="failed",
            title=title,
            message="localized text the client never sees",
            developer_message=title,
            response_code=status,
        ),
    )


@dataclass(slots=True)
class Backend:
    router: respx.Router
    esims: respx.Route
    orders: respx.Route
    refresh: respx.Route

    @property
    def esim_calls(self) -> list[httpx.Request]:
        return [call.request for call in self.router.calls if call.request.url.path.endswith("/user/my-esim")]

    @property
    def order_calls(self) -> list[httpx.Request]:
        return [call.request for call in self.router.calls if "/user/order-history" in call.request.url.path]


@pytest.fixture
def backend(respx_mock: respx.Router) -> Backend:
    mock_login_routes(respx_mock)
    esims = respx_mock.get(MY_ESIM_URL).mock(return_value=httpx.Response(200, json=envelope([esim_payload()])))
    orders = respx_mock.get(ORDER_HISTORY_URL).mock(
        return_value=httpx.Response(200, json=envelope([order_payload()]))
    )
    respx_mock.post(f"{API_URL}/auth/logout").mock(return_value=httpx.Response(200, json=envelope(None)))
    # A read is safe to replay once after a refresh, so the session manager will reach for
    # this route on a 401. Stubbed as a *failure* by default: the interesting default is
    # "the session really is gone", and the success path is asserted explicitly below.
    refresh = respx_mock.post(f"{API_URL}/auth/refresh-token").mock(
        return_value=httpx.Response(401, json=envelope(None, status="failed", title="TOKEN_EXPIRED", response_code=401))
    )
    return Backend(router=respx_mock, esims=esims, orders=orders, refresh=refresh)


@pytest.fixture
async def signed_in(service: AuthenticationService, backend: Backend) -> Backend:
    await sign_in(service)
    return backend


# ------------------------------------------------------------------------ authentication


async def test_listing_esims_requires_an_authenticated_user(
    account_service: AccountService, backend: Backend
) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await account_service.get_my_esims()

    assert not backend.esim_calls, "an unauthenticated call reached the eSIM route"


async def test_listing_orders_requires_an_authenticated_user(
    account_service: AccountService, backend: Backend
) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await account_service.get_order_history()

    assert not backend.order_calls, "an unauthenticated call reached the order route"


async def test_signing_out_ends_access_to_both_reads(
    service: AuthenticationService, account_service: AccountService, signed_in: Backend
) -> None:
    await account_service.get_my_esims()
    await service.logout()

    with pytest.raises(AuthenticationRequiredError):
        await account_service.get_my_esims()
    with pytest.raises(AuthenticationRequiredError):
        await account_service.get_order_history()


# --------------------------------------------------------------------------- session isolation


async def test_one_client_cannot_read_another_clients_esims(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    make_account_service: Callable[[ClientIdentityProvider], AccountService],
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    backend: Backend,
) -> None:
    """Client A signs in; client B has no session and therefore no account to read.

    There is no argument on either tool through which B could name A's account: the bearer
    token comes from B's own session key and from nowhere else.
    """
    await sign_in(make_service(identity_a))

    account_a = make_account_service(identity_a)
    account_b = make_account_service(identity_b)

    assert (await account_a.get_my_esims())["total_count"] == 1

    with pytest.raises(AuthenticationRequiredError):
        await account_b.get_my_esims()
    with pytest.raises(AuthenticationRequiredError):
        await account_b.get_order_history()


async def test_each_client_reads_with_its_own_token(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    make_account_service: Callable[[ClientIdentityProvider], AccountService],
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    backend: Backend,
) -> None:
    """Two signed-in clients send two different device ids, never a shared one."""
    await sign_in(make_service(identity_a), email="a@example.com")
    await sign_in(make_service(identity_b), email="b@example.com")

    await make_account_service(identity_a).get_my_esims()
    await make_account_service(identity_b).get_my_esims()

    device_ids = {request.headers["X-Device-Id"] for request in backend.esim_calls}
    assert len(device_ids) == 2, "two MCP clients shared one device identity"


# -------------------------------------------------------------------------------- eSIMs


async def test_esims_report_the_plan_the_status_and_the_install_details(
    account_service: AccountService, signed_in: Backend
) -> None:
    result = await account_service.get_my_esims()

    assert result["status"] == "ok"
    assert result["total_count"] == 1
    esim = result["esims"][0]
    assert esim["iccid"] == ICCID
    assert esim["label"] == "Trip to France"
    assert esim["order_reference"] == "ord-0001"
    assert esim["plan_started"] is True
    assert esim["bundle_expired"] is False
    assert esim["topup_allowed"] is True
    assert esim["plan"]["name"] == "France 5GB / 30 Days"
    assert esim["plan"]["data"] == "5 GB"
    assert esim["plan"]["validity"] == "30 days"
    assert esim["install"] == {
        "smdp_address": SMDP_ADDRESS,
        "activation_code": ACTIVATION_CODE,
        "qr_code_value": QR_VALUE,
    }


async def test_esim_dates_are_rendered_from_the_backends_epoch_strings(
    account_service: AccountService, signed_in: Backend
) -> None:
    """The backend sends unix seconds as a string; a date read to a user must be a date."""
    result = await account_service.get_my_esims()

    esim = result["esims"][0]
    assert esim["purchased_at"] == "2025-08-06T09:29:14+00:00"
    assert esim["plan_history"][0]["added_at"] == "2025-08-06T09:29:14+00:00"


async def test_an_unreadable_date_is_omitted_rather_than_invented(
    account_service: AccountService, signed_in: Backend
) -> None:
    signed_in.esims.mock(
        return_value=httpx.Response(200, json=envelope([esim_payload(payment_date="not-a-timestamp")]))
    )

    esim = (await account_service.get_my_esims())["esims"][0]

    assert "purchased_at" not in esim


async def test_no_consumption_figure_is_ever_reported(
    account_service: AccountService, signed_in: Backend
) -> None:
    """The backend sends none, so none may appear -- and the model is told so in words."""
    result = await account_service.get_my_esims()

    body = rendered(result).lower()
    for invented in ("data_used", "data_remaining", "remaining_data", "consumption_mb", "used_gb"):
        assert invented not in body
    assert "does not include data usage" in result["consumption_note"].lower()
    assert "never tell the user how much data they have left" in result["consumption_note"].lower()


async def test_the_result_says_this_server_cannot_install_an_esim(
    account_service: AccountService, signed_in: Backend
) -> None:
    result = await account_service.get_my_esims()

    assert "cannot install, activate or transfer" in result["install_note"].lower()
    assert "install credentials" in result["credential_note"].lower()


async def test_an_account_with_no_esims_is_reported_cleanly(
    account_service: AccountService, signed_in: Backend
) -> None:
    signed_in.esims.mock(return_value=httpx.Response(200, json=envelope([])))

    result = await account_service.get_my_esims()

    assert result["status"] == "ok"
    assert result["total_count"] == 0
    assert result["esims"] == []
    assert "no esims yet" in result["message"].lower()
    assert "never say an esim exists" in result["next_step"].lower()


async def test_a_null_esim_payload_is_treated_as_empty_not_as_a_failure(
    account_service: AccountService, signed_in: Backend
) -> None:
    """The backend answers a successful envelope with ``data: null`` for an empty account."""
    signed_in.esims.mock(return_value=httpx.Response(200, json=envelope(None)))

    result = await account_service.get_my_esims()

    assert result["total_count"] == 0


async def test_an_unreadable_esim_row_is_dropped_and_the_rest_survive(
    account_service: AccountService, signed_in: Backend
) -> None:
    signed_in.esims.mock(
        return_value=httpx.Response(200, json=envelope(["not-a-row", esim_payload(), 42]))
    )

    result = await account_service.get_my_esims()

    assert result["total_count"] == 1


# ------------------------------------------------------------------------- order history


async def test_orders_report_the_plan_the_amount_and_the_payment_method(
    account_service: AccountService, signed_in: Backend
) -> None:
    result = await account_service.get_order_history()

    assert result["status"] == "ok"
    assert result["returned_count"] == 1
    order = result["orders"][0]
    assert order["order_reference"] == "ord-0001"
    assert order["order_status"] == "SUCCESS"
    assert order["amount"] == "8.06 USD"
    assert order["currency"] == "USD"
    assert order["payment_method"] == "Card"
    assert order["ordered_at"] == "2025-08-06T09:29:14+00:00"
    assert order["plan"]["name"] == "France 5GB / 30 Days"
    assert order["payment_details"] == {"method": "card", "brand": "visa", "card": "visa ****4242"}


async def test_the_backends_own_paging_parameters_are_passed_through(
    account_service: AccountService, signed_in: Backend
) -> None:
    await account_service.get_order_history(page_index=3, page_size=25)

    query = signed_in.order_calls[0].url.params
    assert query["page_index"] == "3"
    assert query["page_size"] == "25"


async def test_omitting_paging_uses_the_platforms_own_defaults(
    account_service: AccountService, signed_in: Backend
) -> None:
    result = await account_service.get_order_history()

    query = signed_in.order_calls[0].url.params
    assert query["page_index"] == str(DEFAULT_PAGE_INDEX)
    assert query["page_size"] == str(DEFAULT_PAGE_SIZE)
    assert result["page_index"] == DEFAULT_PAGE_INDEX
    assert result["page_size"] == DEFAULT_PAGE_SIZE


@pytest.mark.parametrize("page_index", [0, -1])
async def test_an_impossible_page_number_is_refused_before_any_backend_read(
    account_service: AccountService, signed_in: Backend, page_index: int
) -> None:
    with pytest.raises(InvalidInputError):
        await account_service.get_order_history(page_index=page_index)

    assert not signed_in.order_calls


@pytest.mark.parametrize("page_size", [0, MAX_PAGE_SIZE + 1, 1000])
async def test_an_out_of_range_page_size_is_refused_before_any_backend_read(
    account_service: AccountService, signed_in: Backend, page_size: int
) -> None:
    with pytest.raises(InvalidInputError):
        await account_service.get_order_history(page_size=page_size)

    assert not signed_in.order_calls


async def test_a_full_page_reports_that_more_may_exist(
    account_service: AccountService, signed_in: Backend
) -> None:
    """The backend reports no total, so a full page is the only honest signal of "more"."""
    signed_in.orders.mock(return_value=httpx.Response(200, json=envelope([order_payload()] * 2)))

    result = await account_service.get_order_history(page_size=2)

    assert result["more_available"] is True


async def test_a_partial_page_does_not_claim_more_exist(
    account_service: AccountService, signed_in: Backend
) -> None:
    result = await account_service.get_order_history(page_size=10)

    assert result["more_available"] is False


async def test_an_account_with_no_orders_is_reported_cleanly(
    account_service: AccountService, signed_in: Backend
) -> None:
    signed_in.orders.mock(return_value=httpx.Response(200, json=envelope([])))

    result = await account_service.get_order_history()

    assert result["returned_count"] == 0
    assert result["orders"] == []
    assert "no orders yet" in result["message"].lower()
    assert "never invent one" in result["next_step"].lower()
    assert "more_available" not in result


async def test_an_empty_later_page_says_there_is_nothing_older(
    account_service: AccountService, signed_in: Backend
) -> None:
    """"No orders at all" and "no orders on page 4" are different facts to a user."""
    signed_in.orders.mock(return_value=httpx.Response(200, json=envelope([])))

    result = await account_service.get_order_history(page_index=4)

    assert "no orders on this page" in result["message"].lower()
    assert "no older orders" in result["next_step"].lower()


# --------------------------------------------------------------------- backend failures


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationRequiredError),
        (403, AuthenticationRequiredError),
        (429, RateLimitedError),
        (500, BackendUnavailableError),
        (503, BackendUnavailableError),
    ],
)
async def test_esim_backend_failures_map_to_the_existing_typed_errors(
    account_service: AccountService, signed_in: Backend, status: int, error_type: type[Exception]
) -> None:
    signed_in.esims.mock(return_value=failure_response(status, title="SOMETHING_WENT_WRONG"))

    with pytest.raises(error_type):
        await account_service.get_my_esims()


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationRequiredError),
        (429, RateLimitedError),
        (500, BackendUnavailableError),
    ],
)
async def test_order_backend_failures_map_to_the_existing_typed_errors(
    account_service: AccountService, signed_in: Backend, status: int, error_type: type[Exception]
) -> None:
    signed_in.orders.mock(return_value=failure_response(status, title="SOMETHING_WENT_WRONG"))

    with pytest.raises(error_type):
        await account_service.get_order_history()


async def test_a_transport_failure_is_reported_as_unavailable_not_as_an_empty_account(
    account_service: AccountService, signed_in: Backend
) -> None:
    """"The platform could not be reached" must never read as "you own nothing"."""
    signed_in.esims.mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(BackendUnavailableError):
        await account_service.get_my_esims()


async def test_an_expired_token_is_refreshed_once_and_the_read_is_replayed(
    account_service: AccountService, signed_in: Backend
) -> None:
    """Authentication expiry uses the existing session machinery, not a new code path.

    A read is safe to repeat, so the one allowed refresh-and-replay applies here exactly as
    it does to get_user_profile: the user is never asked to sign in again for a token this
    server could rotate itself.
    """
    from tests.conftest import auth_payload, make_jwt

    signed_in.refresh.mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=make_jwt())))
    )
    responses = [
        failure_response(401, title="TOKEN_EXPIRED"),
        httpx.Response(200, json=envelope([esim_payload()])),
    ]
    signed_in.esims.mock(side_effect=responses)

    result = await account_service.get_my_esims()

    assert result["total_count"] == 1
    assert len(signed_in.esim_calls) == 2, "the read was not replayed after the refresh"
    assert signed_in.refresh.call_count == 1, "more than one refresh was attempted"


async def test_a_refresh_that_fails_asks_the_user_to_sign_in_again(
    account_service: AccountService, signed_in: Backend
) -> None:
    signed_in.esims.mock(return_value=failure_response(401, title="TOKEN_EXPIRED"))

    with pytest.raises(AuthenticationRequiredError):
        await account_service.get_my_esims()


# ------------------------------------------------------------------- secrets and privacy


async def test_no_result_carries_a_receipt_email_a_billing_address_or_a_charge_id(
    account_service: AccountService, signed_in: Backend
) -> None:
    """The closed field set, tested against a payload that really contains all three."""
    body = rendered(await account_service.get_order_history())

    for secret in LEAKABLE:
        assert secret not in body, f"{secret!r} reached an order-history result"


async def test_no_result_carries_a_token(
    account_service: AccountService, signed_in: Backend
) -> None:
    esims = rendered(await account_service.get_my_esims())
    orders = rendered(await account_service.get_order_history())

    for body in (esims, orders):
        lowered = body.lower()
        assert "access_token" not in lowered
        assert "refresh_token" not in lowered
        assert "bearer" not in lowered


async def test_install_credentials_are_never_written_to_a_log(
    account_service: AccountService, signed_in: Backend, caplog: pytest.LogCaptureFixture
) -> None:
    """They are returned to the owner, and they stay out of every log line."""
    with caplog.at_level(logging.DEBUG):
        await account_service.get_my_esims()

    logs = " ".join(record.getMessage() + rendered(record.__dict__) for record in caplog.records)
    assert ACTIVATION_CODE not in logs
    assert QR_VALUE not in logs
    assert ICCID not in logs


async def test_a_receipt_secret_is_never_written_to_a_log(
    account_service: AccountService, signed_in: Backend, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await account_service.get_order_history()

    logs = " ".join(record.getMessage() + rendered(record.__dict__) for record in caplog.records)
    for secret in LEAKABLE:
        assert secret not in logs
