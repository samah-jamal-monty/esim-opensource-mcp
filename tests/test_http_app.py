"""The deployed Streamable HTTP entry point, exercised over a real socket.

These tests start the actual ASGI app under uvicorn on an ephemeral loopback port and
speak the MCP wire protocol to it with a plain HTTP client -- no MCP client library, so
the headers a remote client must send are spelled out and asserted rather than assumed.
That is what a deployment regression looks like from the outside: the endpoint is either
mounted at ``/mcp`` and answers ``initialize``, or it is not.

Both request eras the installed SDK routes between are covered, because a deployed URL is
reached by whichever client the user happens to run:

* the **handshake** era (``2025-*``) -- ``initialize`` returns an ``Mcp-Session-Id`` that
  later requests carry. This is what Claude Desktop and Claude Code send today;
* the **modern** era (``2026-07-28``) -- no handshake, every request carries the protocol
  envelope in ``params._meta`` and repeats it in the routing headers.

No network I/O reaches a backend: the settings point at ``https://backend.test`` and
neither ``initialize`` nor ``tools/list`` calls one.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn
from mcp.shared.inbound import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    MCP_METHOD_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    PROTOCOL_VERSION_META_KEY,
)
from mcp.types import LATEST_PROTOCOL_VERSION
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from esim_mcp import __version__
from esim_mcp.http_app import HEALTH_PATH, MCP_PATH, create_app, resolve_bind_host, resolve_bind_port
from esim_mcp.server import SERVER_NAME
from esim_mcp.settings import Settings
from tests.test_server import EXPECTED_TOOLS

#: Headers every Streamable HTTP POST must carry: the transport rejects a body that is not
#: JSON, and a caller that does not accept both response shapes.
BASE_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

#: Newest handshake-era revision the installed SDK still serves the 2025 flow for.
HANDSHAKE_VERSION = HANDSHAKE_PROTOCOL_VERSIONS[-1]

CLIENT_INFO = {"name": "deployment-integration-test", "version": "1.0"}

SHUTDOWN_TIMEOUT_SECONDS = 10.0


class _NoSignalServer(uvicorn.Server):
    """A uvicorn server that leaves pytest's own signal handlers alone."""

    def install_signal_handlers(self) -> None:  # pragma: no cover - trivial override
        return


def sse_messages(body: str) -> list[dict[str, Any]]:
    """The JSON-RPC messages carried by an SSE response body."""
    return [json.loads(line[len("data:") :].strip()) for line in body.splitlines() if line.startswith("data:")]


def result_of(response: httpx.Response) -> dict[str, Any]:
    """The single JSON-RPC result in a response, whichever encoding was used."""
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        messages = sse_messages(response.text)
    else:
        messages = [response.json()]
    assert len(messages) == 1, response.text
    assert "error" not in messages[0], messages[0]
    return messages[0]["result"]


@pytest.fixture
async def base_url(settings: Settings) -> AsyncIterator[str]:
    """Serve the real ASGI app on an ephemeral port and yield its base URL.

    The listening socket is opened here rather than by uvicorn, so the port is known
    before the server task starts and no readiness polling is needed: a connection made
    while the app is still running its lifespan waits in the accept backlog.
    """
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = _NoSignalServer(uvicorn.Config(create_app(settings), log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=SHUTDOWN_TIMEOUT_SECONDS)
        listener.close()


@pytest.fixture
async def client(base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as http_client:
        yield http_client


async def initialize(client: httpx.AsyncClient) -> httpx.Response:
    """Run the handshake-era ``initialize`` exchange."""
    return await client.post(
        MCP_PATH,
        headers=BASE_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": HANDSHAKE_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        },
    )


async def session_headers(client: httpx.AsyncClient) -> dict[str, str]:
    """Complete the handshake and return the headers subsequent requests must carry."""
    response = await initialize(client)
    result_of(response)
    headers = {
        **BASE_HEADERS,
        "mcp-session-id": response.headers["mcp-session-id"],
        MCP_PROTOCOL_VERSION_HEADER: HANDSHAKE_VERSION,
    }
    notified = await client.post(
        MCP_PATH,
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notified.status_code == 202, notified.text
    return headers


def modern_request(request_id: int, method: str) -> tuple[dict[str, str], dict[str, Any]]:
    """Headers and body for a modern-era request: envelope in ``_meta``, echoed in headers."""
    headers = {**BASE_HEADERS, MCP_PROTOCOL_VERSION_HEADER: LATEST_PROTOCOL_VERSION, MCP_METHOD_HEADER: method}
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {
            "_meta": {
                PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION,
                CLIENT_CAPABILITIES_META_KEY: {},
                CLIENT_INFO_META_KEY: CLIENT_INFO,
            }
        },
    }
    return headers, body


async def test_health_is_200(client: httpx.AsyncClient) -> None:
    """What the platform's health check hits."""
    response = await client.get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": SERVER_NAME, "version": __version__}


async def test_mcp_endpoint_is_mounted(client: httpx.AsyncClient) -> None:
    """The regression this suite exists for: /mcp must not be a 404.

    A client that sends nothing but a JSON body still has to *reach* the endpoint -- the
    failure is a protocol error from the transport, never "no such route".
    """
    response = await client.post(MCP_PATH, content=b"{}", headers={"content-type": "application/json"})

    assert response.status_code != 404, response.text


async def test_initialize_succeeds(client: httpx.AsyncClient) -> None:
    response = await initialize(client)

    result = result_of(response)
    assert response.headers["mcp-session-id"]
    assert result["serverInfo"]["name"] == SERVER_NAME
    assert result["protocolVersion"] == HANDSHAKE_VERSION
    assert result["instructions"]


async def test_tools_list_returns_the_existing_tools(client: httpx.AsyncClient) -> None:
    headers = await session_headers(client)

    response = await client.post(
        MCP_PATH,
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )

    names = {tool["name"] for tool in result_of(response)["tools"]}
    assert names == EXPECTED_TOOLS


async def test_modern_protocol_clients_reach_the_same_tools(client: httpx.AsyncClient) -> None:
    """A per-request-envelope client needs no handshake and sees the same surface."""
    headers, body = modern_request(1, "tools/list")

    response = await client.post(MCP_PATH, headers=headers, json=body)

    names = {tool["name"] for tool in result_of(response)["tools"]}
    assert names == EXPECTED_TOOLS


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
        "/register",
    ],
)
async def test_no_invented_oauth_surface(client: httpx.AsyncClient, path: str) -> None:
    """No OAuth is configured, so its metadata is absent rather than faked.

    A stub here would tell a client this server can issue and verify tokens when it cannot;
    the honest answer from an unconfigured resource server is 404.
    """
    assert (await client.get(path)).status_code == 404


def test_port_prefers_the_servers_own_setting(settings: Settings) -> None:
    """An explicit ESIM_MCP_PORT is the operator's choice and outranks the platform's."""
    resolved = resolve_bind_port(settings, {"ESIM_MCP_PORT": "8080", "PORT": "10000"})

    assert resolved == settings.port


def test_port_falls_back_to_the_platform_port(settings: Settings) -> None:
    """Render (and Heroku, and Cloud Run) hand the port over in PORT."""
    assert resolve_bind_port(settings, {"PORT": "10000"}) == 10000


def test_port_defaults_when_nothing_is_set(settings: Settings) -> None:
    assert resolve_bind_port(settings, {}) == settings.port


@pytest.mark.parametrize("value", ["", "not-a-port", "0", "70000"])
def test_a_broken_platform_port_fails_loudly(settings: Settings, value: str) -> None:
    """Better a refused start than a process listening somewhere nobody routes to."""
    with pytest.raises(ValueError):
        resolve_bind_port(settings, {"PORT": value})


def test_bind_host_is_the_configured_host(settings: Settings) -> None:
    assert resolve_bind_host(settings, {"ESIM_MCP_HOST": "0.0.0.0"}) == settings.host


def test_public_bind_drops_the_loopback_only_host_allow_list(settings: Settings) -> None:
    """0.0.0.0 must not inherit the SDK's localhost-only Host allow-list.

    With it on, every request arriving through a platform's proxy (Host: the public
    hostname) would be answered with 421 instead of being served.
    """
    public = Settings.build(
        api_base_url=settings.api_base_url,
        environment="qa",
        device_id_salt="deployment-test-salt-value-long-enough-0123456789",
        host="0.0.0.0",  # the configuration under test
    )

    app = create_app(public)

    assert app.state.esim_mcp.server.session_manager.security_settings is None


def test_loopback_bind_keeps_host_validation(settings: Settings) -> None:
    """Locally the SDK's DNS-rebinding protection stays on, as it does upstream."""
    app = create_app(settings)

    security = app.state.esim_mcp.server.session_manager.security_settings
    assert security is not None
    assert security.enable_dns_rebinding_protection
