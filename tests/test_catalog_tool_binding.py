"""The MCP binding itself: arguments really reach the service, results really come back.

Every other catalogue test drives :class:`CatalogService` directly, which would not notice
a tool registered with a mis-wired argument. These tests go through
``MCPServer.call_tool``, so the schema, the binding and the result serialization are all
exercised the way a real client exercises them.
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
    FRANCE_GUID,
    StubIdentityProvider,
    bundle_payload,
    envelope,
    home_payload,
)

BUNDLE_CODE = "aaaaaaaa-0000-4000-8000-000000000001"


@pytest.fixture
async def server(settings: Settings) -> AsyncIterator[MCPServer]:
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        yield components.server
    finally:
        await components.aclose()


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


async def test_country_search_works_end_to_end_through_the_tool_interface(
    server: MCPServer, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/bundles/countries").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
    )
    bundles_route = respx_mock.get(f"{API_URL}/bundles/by-country").mock(
        return_value=httpx.Response(200, json=envelope([bundle_payload()]))
    )

    result = await server.call_tool("find_bundles_by_country", {"country": "France"})

    assert result.is_error is not True
    body = payload_of(result)
    assert body["country"]["country"] == "France"
    assert body["bundles"][0]["bundle_code"] == BUNDLE_CODE
    assert bundles_route.calls.last.request.url.params["country_codes"] == FRANCE_GUID


async def test_every_optional_argument_is_wired_through(server: MCPServer, respx_mock: respx.Router) -> None:
    respx_mock.get(f"{API_URL}/bundles/countries").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
    )
    route = respx_mock.get(f"{API_URL}/bundles/by-country").mock(
        return_value=httpx.Response(
            200,
            json=envelope(
                [
                    bundle_payload(bundle_code="small", gprs_limit=1.0, price=5.0),
                    bundle_payload(bundle_code="large", gprs_limit=10.0, price=20.0),
                ]
            ),
        )
    )

    result = await server.call_tool(
        "find_bundles_by_country",
        {
            "country": "FR",
            "minimum_data_gb": 5,
            "max_price": 25,
            "minimum_validity_days": 30,
            "unlimited_only": False,
            "sort_by": "price",
            "limit": 3,
            "locale": "fr",
            "currency": "eur",
        },
    )

    body = payload_of(result)
    assert [bundle["bundle_code"] for bundle in body["bundles"]] == ["large"]
    assert body["filters_applied"]
    request = route.calls.last.request
    assert request.headers["Accept-Language"] == "fr"
    assert request.headers["X-Currency"] == "EUR"


async def test_bundle_details_work_through_the_tool_interface(server: MCPServer, respx_mock: respx.Router) -> None:
    respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload()))
    )

    body = payload_of(await server.call_tool("get_bundle_details", {"bundle_code": BUNDLE_CODE}))

    assert body["bundle"]["bundle_code"] == BUNDLE_CODE
    assert body["bundle"]["price_note"]


@pytest.mark.parametrize(
    ("tool_name", "arguments", "route_path", "response"),
    [
        ("list_countries", {}, "/bundles/countries", CATALOG_COUNTRIES),
        ("list_regions", {}, "/bundles/region", CATALOG_REGIONS),
        ("browse_home_catalog", {}, "/home/", None),
        ("list_cruise_bundles", {}, "/home/cruise", None),
    ],
)
async def test_the_remaining_catalogue_tools_are_callable(
    server: MCPServer,
    respx_mock: respx.Router,
    tool_name: str,
    arguments: dict[str, Any],
    route_path: str,
    response: Any,
) -> None:
    respx_mock.get(f"{API_URL}{route_path}").mock(
        return_value=httpx.Response(200, json=envelope(response if response is not None else home_payload()))
    )

    result = await server.call_tool(tool_name, arguments)

    assert result.is_error is not True
    assert payload_of(result)["status"] == "ok"


async def test_an_unknown_destination_comes_back_as_a_safe_actionable_error(
    server: MCPServer, respx_mock: respx.Router
) -> None:
    """The suggestions have to survive the MCP error channel.

    The SDK turns a failing tool into ``CallToolResult(is_error=True)`` whose only content
    is ``str(exception)`` (``_handle_call_tool``), so the text asserted here is verbatim
    what a client receives -- which is why the suggestions live in the message and not in a
    structured payload.
    """
    respx_mock.get(f"{API_URL}/bundles/countries").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
    )

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("find_bundles_by_country", {"country": "Fren"})

    rendered = str(excinfo.value)
    assert "country_not_found" in rendered
    assert "French Guiana" in rendered
    assert "Traceback" not in rendered


async def test_a_catalogue_outage_comes_back_as_a_safe_error(server: MCPServer, respx_mock: respx.Router) -> None:
    respx_mock.get(f"{API_URL}/home/").mock(
        return_value=httpx.Response(
            500,
            json=envelope(
                None,
                status="failed",
                title="Exception",
                developer_message="psycopg2 connection refused at 10.0.0.4",
                response_code=500,
            ),
        )
    )

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("browse_home_catalog", {})

    rendered = str(excinfo.value)
    assert "catalog_unavailable" in rendered
    assert "try again shortly" in rendered
    assert "psycopg2" not in rendered
    assert "10.0.0.4" not in rendered
    assert "Traceback" not in rendered


async def test_an_invalid_argument_never_echoes_backend_or_input_detail(
    server: MCPServer, respx_mock: respx.Router
) -> None:
    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("find_bundles_by_country", {"country": "France", "sort_by": "cheapness"})

    rendered = str(excinfo.value)
    assert "invalid_input" in rendered
    assert "'price'" in rendered
    assert not respx_mock.calls


async def test_browsing_through_the_tool_interface_needs_no_session(
    server: MCPServer, respx_mock: respx.Router
) -> None:
    """A client that never logged in can still browse: no auth route is touched."""
    respx_mock.get(f"{API_URL}/bundles/countries").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
    )
    respx_mock.get(f"{API_URL}/bundles/by-country").mock(
        return_value=httpx.Response(200, json=envelope([bundle_payload()]))
    )

    status = payload_of(await server.call_tool("get_login_status", {}))
    result = await server.call_tool("find_bundles_by_country", {"country": "France"})

    assert status["authenticated"] is False
    assert result.is_error is not True
    assert all("/auth/" not in call.request.url.path for call in respx_mock.calls)
