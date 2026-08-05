"""The single wallet read: its route, its headers, and how its money is parsed.

The backend declares ``UserWalletResponse.balance`` as a ``float`` and answers ``data: null``
when the user has no wallet row, so both of those have to be handled exactly rather than
approximately -- a wrong balance here would become a wrong number read out to a user.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr

from esim_mcp.client.wallet import WalletApiClient
from esim_mcp.errors import AuthenticationRequiredError, BackendTimeoutError, InvalidBackendResponseError
from esim_mcp.models.wallet import UserWallet, decimal_from_number, parse_user_wallet
from tests.conftest import API_URL, envelope, wallet_payload

WALLET_URL = f"{API_URL}/wallet/user_wallet_by_user"
TOKEN = SecretStr("access-token-value")


async def read(client: WalletApiClient, *, currency: str = "USD") -> UserWallet | None:
    return await client.get_user_wallet(device_id="device-1", access_token=TOKEN, locale="en", currency=currency)


# --------------------------------------------------------------------------- the request


async def test_the_authenticated_per_user_route_is_used(
    wallet_client: WalletApiClient, respx_mock: respx.Router
) -> None:
    route = respx_mock.get(WALLET_URL).mock(return_value=httpx.Response(200, json=envelope(wallet_payload())))

    await read(wallet_client)

    assert route.called
    assert route.calls.last.request.url.path == "/api/v1/wallet/user_wallet_by_user"


async def test_the_request_carries_the_bearer_token_the_device_id_and_the_currency(
    wallet_client: WalletApiClient, respx_mock: respx.Router
) -> None:
    route = respx_mock.get(WALLET_URL).mock(return_value=httpx.Response(200, json=envelope(wallet_payload())))

    await read(wallet_client, currency="EUR")

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer access-token-value"
    assert request.headers["X-Device-Id"] == "device-1"
    assert request.headers["X-Currency"] == "EUR"


async def test_the_read_is_retried_on_a_gateway_failure(
    wallet_client: WalletApiClient, respx_mock: respx.Router
) -> None:
    """A balance read is idempotent, so the shared bounded retry applies."""
    route = respx_mock.get(WALLET_URL).mock(
        side_effect=[
            httpx.Response(503, json=envelope(None, status="failed", response_code=503)),
            httpx.Response(200, json=envelope(wallet_payload(balance=12.5))),
        ]
    )

    wallet = await read(wallet_client)

    assert route.call_count == 2
    assert wallet.balance == Decimal("12.5")


# ---------------------------------------------------------------------------- responses


async def test_a_wallet_is_parsed_into_an_exact_decimal(
    wallet_client: WalletApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(WALLET_URL).mock(
        return_value=httpx.Response(200, json=envelope(wallet_payload(balance=20.0, currency="usd")))
    )

    wallet = await read(wallet_client)

    assert wallet.balance == Decimal("20.0")
    assert isinstance(wallet.balance, Decimal)
    assert wallet.currency == "USD"


async def test_no_wallet_is_reported_as_none_rather_than_zero(
    wallet_client: WalletApiClient, respx_mock: respx.Router
) -> None:
    """The backend returns a *successful* envelope with ``data: null`` for a missing wallet."""
    respx_mock.get(WALLET_URL).mock(return_value=httpx.Response(200, json=envelope(None)))

    assert await read(wallet_client) is None


async def test_a_malformed_wallet_payload_is_refused(wallet_client: WalletApiClient, respx_mock: respx.Router) -> None:
    respx_mock.get(WALLET_URL).mock(
        return_value=httpx.Response(200, json=envelope({"balance": "not-a-number", "currency": "USD"}))
    )

    with pytest.raises(InvalidBackendResponseError):
        await read(wallet_client)


async def test_a_non_object_wallet_payload_is_refused(wallet_client: WalletApiClient, respx_mock: respx.Router) -> None:
    respx_mock.get(WALLET_URL).mock(return_value=httpx.Response(200, json=envelope([1, 2, 3])))

    with pytest.raises(InvalidBackendResponseError):
        await read(wallet_client)


async def test_an_expired_token_surfaces_as_an_authentication_error(
    wallet_client: WalletApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(WALLET_URL).mock(
        return_value=httpx.Response(401, json=envelope(None, status="failed", response_code=401))
    )

    with pytest.raises(AuthenticationRequiredError):
        await read(wallet_client)


async def test_a_backend_timeout_surfaces_as_a_safe_error(
    wallet_client: WalletApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(WALLET_URL).mock(side_effect=httpx.ConnectTimeout("too slow"))

    with pytest.raises(BackendTimeoutError) as excinfo:
        await read(wallet_client)

    assert "too slow" not in str(excinfo.value)


async def test_a_backend_error_never_leaks_its_developer_message(
    wallet_client: WalletApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(WALLET_URL).mock(
        return_value=httpx.Response(
            500,
            json=envelope(
                None,
                status="failed",
                title="Exception",
                developer_message="psycopg2 error at 10.0.0.7 for user_wallet.amount",
                response_code=500,
            ),
        )
    )

    with pytest.raises(Exception) as excinfo:
        await read(wallet_client)

    rendered = str(excinfo.value)
    assert "psycopg2" not in rendered
    assert "10.0.0.7" not in rendered


# ------------------------------------------------------------------------ number parsing


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        (8.06, Decimal("8.06")),
        (20, Decimal("20")),
        ("8.06", Decimal("8.06")),
        (Decimal("8.06"), Decimal("8.06")),
        (0, Decimal("0")),
        (0.1, Decimal("0.1")),
    ],
)
def test_backend_numbers_become_exact_decimals(supplied: object, expected: Decimal) -> None:
    """``Decimal(8.06)`` is ``8.0599999...``; going through ``str`` is what keeps it exact."""
    assert decimal_from_number(supplied) == expected


def test_the_float_route_would_have_been_wrong() -> None:
    assert decimal_from_number(8.06) == Decimal("8.06")
    # RUF032 flags exactly the mistake being demonstrated here: constructing a Decimal
    # straight from a float is what produces 8.0599999..., which is why the parser goes
    # through str() instead.
    assert Decimal(8.06) != Decimal("8.06")  # noqa: RUF032


@pytest.mark.parametrize("supplied", [None, True, False, "", "abc", float("nan"), float("inf")])
def test_unusable_numbers_are_rejected_rather_than_guessed(supplied: object) -> None:
    assert decimal_from_number(supplied) is None


def test_a_wallet_without_a_currency_still_parses() -> None:
    """``currency`` is documented as required, but a blank one must not lose the balance."""
    wallet = parse_user_wallet({"balance": 5.0, "currency": ""})

    assert wallet.balance == Decimal("5.0")
    assert wallet.currency == ""


def test_a_wallet_payload_ignores_unknown_backend_fields() -> None:
    wallet = parse_user_wallet({"balance": 5.0, "currency": "USD", "user_id": "u-1", "future_field": 1})

    assert wallet.balance == Decimal("5.0")
    assert not hasattr(wallet, "user_id")
