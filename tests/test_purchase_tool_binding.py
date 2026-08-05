"""The MCP binding for the Phase 3 tools: arguments reach the service, results come back.

The service-level tests drive :class:`PurchasePreparationService` directly, which would not
notice a tool registered with a mis-wired argument. These go through ``MCPServer.call_tool``,
so the schema, the binding, the error channel and the result serialization are exercised the
way a real client exercises them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from esim_mcp.server import build_components
from esim_mcp.settings import Settings
from tests.conftest import (
    API_URL,
    CATALOG_COUNTRIES,
    CATALOG_REGIONS,
    StubIdentityProvider,
    auth_payload,
    bundle_payload,
    envelope,
    make_jwt,
    wallet_payload,
)

BUNDLE_CODE = "aaaaaaaa-0000-4000-8000-000000000001"


def payload_of(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("tool returned no readable content")


@pytest.fixture
async def server(settings: Settings) -> AsyncIterator[MCPServer]:
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        yield components.server
    finally:
        await components.aclose()


@pytest.fixture
def routes(respx_mock: respx.Router) -> respx.Router:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=make_jwt())))
    )
    respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload(bundle_code=BUNDLE_CODE, price=8.06)))
    )
    respx_mock.get(f"{API_URL}/wallet/user_wallet_by_user").mock(
        return_value=httpx.Response(200, json=envelope(wallet_payload(balance=20.0)))
    )
    respx_mock.get(f"{API_URL}/bundles/countries").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
    )
    respx_mock.get(f"{API_URL}/bundles/region").mock(return_value=httpx.Response(200, json=envelope(CATALOG_REGIONS)))
    return respx_mock


async def login(server: MCPServer) -> None:
    await server.call_tool("request_login_otp", {"email": "person@example.com"})
    await server.call_tool("verify_login_otp", {"verification_pin": "123456", "email": "person@example.com"})


# --------------------------------------------------------------------------- happy paths


async def test_a_wallet_quote_works_end_to_end_through_the_tool_interface(
    server: MCPServer, routes: respx.Router
) -> None:
    await login(server)

    result = await server.call_tool(
        "prepare_purchase",
        {"bundle_code": BUNDLE_CODE, "payment_method": "Wallet", "country": "France"},
    )

    assert result.is_error is not True
    body = payload_of(result)
    assert body["status"] == "prepared"
    assert body["pricing"]["displayed_amount"] == "8.06"
    assert body["wallet"]["estimated_remaining_balance"] == "11.94"
    assert body["search_context"]["country"] == "France"
    assert body["order_created"] is False
    assert body["charged"] is False


async def test_every_optional_argument_is_wired_through(server: MCPServer, routes: respx.Router) -> None:
    await login(server)

    body = payload_of(
        await server.call_tool(
            "prepare_purchase",
            {
                "bundle_code": BUNDLE_CODE,
                "payment_method": "Card",
                "region": "Europe",
                "locale": "fr",
                "currency": "eur",
            },
        )
    )

    assert body["search_context"]["kind"] == "region"
    # The locale and currency have to reach the *bundle* read: that is the request whose
    # answer becomes the quoted price. (The region lookup carries only the locale, exactly as
    # the catalogue client sends it.)
    bundle_request = next(
        call.request for call in reversed(routes.calls) if call.request.url.path.endswith(BUNDLE_CODE)
    )
    assert bundle_request.headers["Accept-Language"] == "fr"
    assert bundle_request.headers["X-Currency"] == "EUR"


async def test_the_read_and_cancel_tools_work_end_to_end(server: MCPServer, routes: respx.Router) -> None:
    await login(server)
    prepared = payload_of(
        await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Card"})
    )

    read = payload_of(await server.call_tool("get_prepared_purchase", {"quote_id": prepared["quote_id"]}))
    cancelled = payload_of(await server.call_tool("cancel_prepared_purchase", {"quote_id": prepared["quote_id"]}))

    assert read["quote_id"] == prepared["quote_id"]
    assert cancelled["status"] == "cancelled"
    assert cancelled["order_cancelled"] is False


# ------------------------------------------------------------------------- error channel


async def test_preparing_without_a_session_is_a_safe_actionable_error(server: MCPServer, routes: respx.Router) -> None:
    """The SDK renders a failing tool as ``str(exception)``, so the message is the contract."""
    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Wallet"})

    rendered = str(excinfo.value)
    assert "authentication_required" in rendered
    assert "Traceback" not in rendered


async def test_an_inactive_bundle_is_a_safe_actionable_error(server: MCPServer, routes: respx.Router) -> None:
    await login(server)
    routes.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload(bundle_code=BUNDLE_CODE, is_active=False)))
    )

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Card"})

    rendered = str(excinfo.value)
    assert "bundle_unavailable" in rendered
    assert "search that destination again" in rendered


async def test_an_unsupported_payment_method_is_a_safe_actionable_error(
    server: MCPServer, routes: respx.Router
) -> None:
    await login(server)

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "DCB"})

    rendered = str(excinfo.value)
    assert "unsupported_payment_method" in rendered
    assert "Wallet" in rendered and "Card" in rendered


async def test_a_wallet_the_backend_cannot_report_is_a_safe_actionable_error(
    server: MCPServer, routes: respx.Router
) -> None:
    await login(server)
    routes.get(f"{API_URL}/wallet/user_wallet_by_user").mock(
        return_value=httpx.Response(
            500,
            json=envelope(
                None,
                status="failed",
                title="Exception",
                developer_message="psycopg2 connection refused at 10.0.0.9",
                response_code=500,
            ),
        )
    )

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Wallet"})

    rendered = str(excinfo.value)
    assert "psycopg2" not in rendered
    assert "10.0.0.9" not in rendered
    assert "Traceback" not in rendered


async def test_an_unknown_quote_reference_is_a_safe_error(server: MCPServer, routes: respx.Router) -> None:
    await login(server)

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("get_prepared_purchase", {"quote_id": "made-up-reference"})

    rendered = str(excinfo.value)
    assert "quote_not_found" in rendered
    assert "Prepare the plan again" in rendered


async def test_a_missing_required_argument_is_rejected_by_the_schema(server: MCPServer, routes: respx.Router) -> None:
    await login(server)

    with pytest.raises(Exception):  # noqa: B017 - the SDK's own validation error
        await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE})


# ------------------------------------------------------------------------- quote secrecy


async def test_the_quote_reference_leaks_nothing_about_the_user_or_the_plan(
    server: MCPServer, routes: respx.Router
) -> None:
    await login(server)

    body = payload_of(
        await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Wallet"})
    )

    quote_id = body["quote_id"]
    for secret in (BUNDLE_CODE, "person@example.com", "b3f1c0de", "8.06", "20.00"):
        assert secret not in quote_id


async def test_one_client_cannot_read_another_clients_quote_through_the_tool_interface(
    settings: Settings, routes: respx.Router
) -> None:
    """Two servers, two verified identities, one shared backend. Quotes must not cross."""
    first = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    second = build_components(settings, identity_provider=StubIdentityProvider("client-b"))
    try:
        await login(first.server)
        await login(second.server)
        prepared = payload_of(
            await first.server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Card"})
        )

        with pytest.raises(ToolError) as excinfo:
            await second.server.call_tool("get_prepared_purchase", {"quote_id": prepared["quote_id"]})
    finally:
        await first.aclose()
        await second.aclose()

    assert "quote_not_found" in str(excinfo.value)


# -------------------------------------------------------------- in-memory loss on restart


async def test_quotes_do_not_survive_a_server_restart(settings: Settings, routes: respx.Router) -> None:
    """Documented behaviour, asserted so it stays a *known* limitation rather than a surprise.

    Losing a quote costs the user a re-prepare and nothing else: a quote holds no money, no
    reservation and no backend order.
    """
    first = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        await login(first.server)
        prepared = payload_of(
            await first.server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Card"})
        )
    finally:
        await first.aclose()

    restarted = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        await login(restarted.server)

        with pytest.raises(ToolError) as excinfo:
            await restarted.server.call_tool("get_prepared_purchase", {"quote_id": prepared["quote_id"]})
    finally:
        await restarted.aclose()

    assert "quote_not_found" in str(excinfo.value)
