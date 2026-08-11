"""Cross-user session isolation, driven through TWO real Streamable HTTP MCP clients.

This is the regression suite for a reproduced production defect: user A signed in over the
deployed connector, a different user B connected, and B was served as A -- same session, same
eSIM account, same bearer token.

Why these tests use the real transport rather than calling services directly
----------------------------------------------------------------------------
The bug was invisible at the service layer. Every service was already keyed on
``identity.session_key``, and the unit tests injected a ``StubIdentityProvider`` per caller,
so isolation looked correct everywhere. The defect lived one layer below, in what the *real*
transport resolved that key to: with no OAuth principal, every connection fell back to one
constant development identity, so every user got one key.

A test that stubs identity can therefore never catch this class of bug. These tests stand up
the actual ASGI app, open two independent ``ClientSession``s over Streamable HTTP -- two
connections, two MCP session ids, exactly as two ChatGPT users would -- and assert on what
each client can see.

Nothing here reaches the eSIM platform: it is replaced by a local stub that hands each
account its own bearer token. No purchase, no provisioning, no real token.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from esim_mcp.http_app import MCP_PATH, create_app
from esim_mcp.settings import Settings
from tests.conftest import CATALOG_REGIONS, TEST_SALT, bundle_payload, envelope, make_jwt

pytestmark = pytest.mark.anyio

#: Two distinct eSIM accounts, so "B saw A's account" is observable rather than inferred.
ACCOUNTS: dict[str, dict[str, Any]] = {
    "alice@example.com": {"first_name": "Alice", "balance": 111.0, "subject": "user-alice"},
    "bob@example.com": {"first_name": "Bob", "balance": 222.0, "subject": "user-bob"},
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _account_payload(email: str, access_token: str) -> dict[str, Any]:
    account = ACCOUNTS[email]
    return {
        "access_token": access_token,
        "refresh_token": f"refresh-{account['subject']}",
        "user_token": account["subject"],
        "is_verified": True,
        "user_info": {
            "is_verified": True,
            "email": email,
            "msisdn": None,
            "first_name": account["first_name"],
            "last_name": "Tester",
            "currency_code": "USD",
            "balance": account["balance"],
            "language": "en",
        },
    }


class StubBackend:
    """A stand-in eSIM platform that hands each account its own bearer token.

    The token-to-account map is what makes leakage visible: ``/auth/user-info`` answers
    according to the ``Authorization`` header it was given, so a client that ends up holding
    another client's token reads that other account back.
    """

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}
        self.logouts: list[str] = []
        self.refreshes: list[str] = []
        self._device_ids: list[str] = []
        self.region_device_ids: list[str] = []

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/ping", self._ping, methods=["GET"]),
                Route("/api/v1/auth/login", self._login, methods=["POST"]),
                Route("/api/v1/auth/verify_otp", self._verify_otp, methods=["POST"]),
                Route("/api/v1/auth/user-info", self._user_info, methods=["GET"]),
                Route("/api/v1/auth/refresh-token", self._refresh, methods=["POST"]),
                Route("/api/v1/auth/logout", self._logout, methods=["POST"]),
                Route("/api/v1/bundles/region", self._regions, methods=["GET"]),
                Route("/api/v1/bundles/by-region/{region_code}", self._bundles_by_region, methods=["GET"]),
            ]
        )

    def device_ids(self) -> set[str]:
        """Every distinct ``X-Device-Id`` this stub has been shown."""
        return set(self._device_ids)

    def _email_for(self, request: Request) -> str | None:
        bearer = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        return self.tokens.get(bearer)

    def _record(self, request: Request) -> None:
        device_id = request.headers.get("x-device-id")
        if device_id:
            self._device_ids.append(device_id)

    async def _ping(self, request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def _login(self, request: Request) -> JSONResponse:
        self._record(request)
        return JSONResponse(envelope(None))

    async def _verify_otp(self, request: Request) -> JSONResponse:
        self._record(request)
        # The backend contract names this user_email on verify, not email.
        body = await request.json()
        email = ((body.get("user_email") or body.get("email")) or "").lower()
        if email not in ACCOUNTS:
            return JSONResponse(envelope(None, status="failed", response_code=400), status_code=400)
        token = make_jwt(subject=ACCOUNTS[email]["subject"], signature=email)
        self.tokens[token] = email
        return JSONResponse(envelope(_account_payload(email, token)))

    async def _user_info(self, request: Request) -> JSONResponse:
        self._record(request)
        email = self._email_for(request)
        if email is None:
            return JSONResponse(envelope(None, status="failed", response_code=401), status_code=401)
        bearer = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        return JSONResponse(envelope(_account_payload(email, bearer)))

    async def _refresh(self, request: Request) -> JSONResponse:
        """Rotate only the token that was presented, and record whose it was."""
        self._record(request)
        stale = (request.headers.get("x-refresh-token") or "").strip()
        email = next((mail for mail in ACCOUNTS if stale == f"refresh-{ACCOUNTS[mail]['subject']}"), None)
        if email is None:
            return JSONResponse(envelope(None, status="failed", response_code=401), status_code=401)
        self.refreshes.append(email)
        rotated = make_jwt(subject=ACCOUNTS[email]["subject"], signature=f"rotated-{email}")
        self.tokens[rotated] = email
        return JSONResponse(envelope(_account_payload(email, rotated)))

    async def _logout(self, request: Request) -> JSONResponse:
        self._record(request)
        email = self._email_for(request)
        if email is not None:
            self.logouts.append(email)
        return JSONResponse(envelope(None))

    # ------------------------------------------------------------------ catalogue routes

    async def _regions(self, request: Request) -> JSONResponse:
        """``GET /bundles/region`` -- device-scoped, and deliberately not token-scoped.

        Browsing needs no login, so this route ignores ``Authorization`` exactly as the real
        one does. It still records the device id, which is what makes a shared identity
        visible.
        """
        self._record(request)
        return JSONResponse(envelope(CATALOG_REGIONS, total_count=len(CATALOG_REGIONS)))

    async def _bundles_by_region(self, request: Request) -> JSONResponse:
        """``GET /bundles/by-region/{region_code}`` -- case-sensitive, like the real one."""
        self._record(request)
        device_id = request.headers.get("x-device-id") or ""
        self.region_device_ids.append(device_id)
        code = request.path_params["region_code"]
        known = {region["region_code"] for region in CATALOG_REGIONS}
        if code not in known:
            return JSONResponse(
                envelope(None, status="failed", title="Request failed.", response_code=400, developer_message=None),
                status_code=400,
            )
        bundles = [bundle_payload(bundle_code=f"{code.lower()}-5gb", gprs_limit=5.0, price=12.5, validity=30)]
        return JSONResponse(envelope(bundles, total_count=len(bundles)))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve(app: Any, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    return server


async def _await_ready(port: int, path: str) -> None:
    async with httpx.AsyncClient() as probe:
        for _ in range(200):
            try:
                await probe.get(f"http://127.0.0.1:{port}{path}", timeout=1.0)
                return
            except Exception:
                await asyncio.sleep(0.05)
    raise RuntimeError(f"server on port {port} never became ready")


@asynccontextmanager
async def two_clients(_unused: Any = None) -> AsyncIterator[tuple[ClientSession, ClientSession, StubBackend]]:
    """Two independent Streamable HTTP connections against ONE server process.

    Real sockets, real uvicorn, the real ASGI app and the real MCP client -- the same shape
    as two ChatGPT users on one deployment. The eSIM platform is a local stub, so nothing
    here reaches QA and nothing can be bought.
    """
    async with one_server() as (url, backend), mcp_client(url) as client_a, mcp_client(url) as client_b:
        yield client_a, client_b, backend


@asynccontextmanager
async def one_server() -> AsyncIterator[tuple[str, StubBackend]]:
    backend = StubBackend()
    backend_port = _free_port()
    mcp_port = _free_port()

    settings = Settings.build(
        api_base_url=f"http://127.0.0.1:{backend_port}",
        environment="development",
        transport="streamable-http",
        device_id_salt=TEST_SALT,
        host="127.0.0.1",
    )
    backend_server = _serve(backend.app(), backend_port)
    mcp_server = _serve(create_app(settings), mcp_port)
    try:
        await _await_ready(backend_port, "/ping")
        await _await_ready(mcp_port, "/health")
        yield f"http://127.0.0.1:{mcp_port}{MCP_PATH}", backend
    finally:
        backend_server.should_exit = True
        mcp_server.should_exit = True
        await asyncio.sleep(0.2)


@asynccontextmanager
async def mcp_client(url: str) -> AsyncIterator[ClientSession]:
    async with (
        streamable_http_client(url) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def call(session: ClientSession, tool: str, **arguments: Any) -> dict[str, Any] | str:
    result = await session.call_tool(tool, arguments)
    for block in result.content:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return {}


async def sign_in(session: ClientSession, email: str) -> dict[str, Any] | str:
    await call(session, "request_login_otp", email=email)
    return await call(session, "verify_login_otp", email=email, verification_pin="123456")


# ------------------------------------------------------------------ the reported defect


async def test_user_a_login_does_not_authenticate_user_b() -> None:
    """The reported production defect, asserted end to end.

    Before the fix this failed: B reported ``authenticated: true`` without ever having
    signed in, because both connections resolved to one constant identity.
    """
    async with two_clients() as (client_a, client_b, _):
        before = await call(client_b, "get_login_status")
        assert before["authenticated"] is False

        assert (await sign_in(client_a, "alice@example.com"))["status"] == "authenticated"

        after = await call(client_b, "get_login_status")
        assert after["authenticated"] is False, "user B inherited user A's authenticated session"
        assert after["status"] == "unauthenticated"


async def test_user_b_cannot_read_user_as_profile() -> None:
    """The consequence that mattered: B reading A's account."""
    async with two_clients() as (client_a, client_b, _):
        await sign_in(client_a, "alice@example.com")

        result = await call(client_b, "get_user_profile")

        assert isinstance(result, str), "an unauthenticated client received a profile"
        assert "authentication_required" in result
        assert "Alice" not in result


# --------------------------------------------------------- two users, two live accounts


async def test_two_clients_hold_two_different_accounts_at_once() -> None:
    async with two_clients() as (client_a, client_b, _):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        profile_a = await call(client_a, "get_user_profile")
        profile_b = await call(client_b, "get_user_profile")

        assert profile_a["user"]["first_name"] == "Alice"
        assert profile_b["user"]["first_name"] == "Bob"
        assert profile_a["wallet"]["balance"] == 111.0
        assert profile_b["wallet"]["balance"] == 222.0
        assert profile_a["user"] != profile_b["user"]


async def test_each_client_sends_its_own_device_id_to_the_platform() -> None:
    """The device id is derived from identity, so a shared identity would show up here."""
    async with two_clients() as (client_a, client_b, backend):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")
        await call(client_a, "get_user_profile")
        await call(client_b, "get_user_profile")

        assert len(backend.device_ids()) >= 2, "two MCP clients presented one device identity"


# ------------------------------------------------------------------------ tool coverage


async def test_account_reads_are_isolated() -> None:
    """eSIM and order reads must be refused for a client that never signed in."""
    async with two_clients() as (client_a, client_b, _):
        await sign_in(client_a, "alice@example.com")

        for tool in ("get_my_esims", "get_order_history", "get_user_profile"):
            result = await call(client_b, tool)
            assert isinstance(result, str) and "authentication_required" in result, (
                f"{tool} served an unauthenticated client"
            )


async def test_purchase_preparation_is_refused_for_an_unauthenticated_client() -> None:
    """The money-adjacent path must not inherit a session either."""
    async with two_clients() as (client_a, client_b, _):
        await sign_in(client_a, "alice@example.com")

        result = await call(client_b, "prepare_purchase", bundle_code="anything", payment_method="Wallet")

        assert isinstance(result, str)
        assert "authentication_required" in result


# ---------------------------------------------------------------------------- lifecycle


async def test_logout_of_a_is_not_a_logout_of_b() -> None:
    async with two_clients() as (client_a, client_b, backend):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        await call(client_a, "logout")

        assert (await call(client_a, "get_login_status"))["authenticated"] is False
        status_b = await call(client_b, "get_login_status")
        assert status_b["authenticated"] is True, "logging out one user signed the other out"
        assert (await call(client_b, "get_user_profile"))["user"]["first_name"] == "Bob"
        assert backend.logouts == ["alice@example.com"]


async def test_token_refresh_for_a_does_not_touch_b() -> None:
    """A rotation on one session must not rotate, invalidate or re-point the other."""
    async with two_clients() as (client_a, client_b, backend):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        app_state_profile_b_before = await call(client_b, "get_user_profile")

        # Force A's session to refresh by expiring its access token server-side, then reading.
        backend.tokens = {
            token: email for token, email in backend.tokens.items() if email != "alice@example.com"
        }
        result_a = await call(client_a, "get_user_profile")

        assert backend.refreshes == ["alice@example.com"], "the wrong session was refreshed"
        assert result_a["user"]["first_name"] == "Alice"

        profile_b_after = await call(client_b, "get_user_profile")
        assert profile_b_after == app_state_profile_b_before, "user B's session changed with user A's refresh"


# --------------------------------------------------------------------------- concurrency


async def test_concurrent_calls_from_two_clients_stay_isolated() -> None:
    """Interleaved traffic must never cross sessions.

    The identity is resolved per call from the connection's own session object, so this is
    also the test that would catch a ContextVar or module-global reintroduced anywhere in the
    request path.
    """
    async with two_clients() as (client_a, client_b, _):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        results = await asyncio.gather(
            *(call(client_a, "get_user_profile") for _ in range(5)),
            *(call(client_b, "get_user_profile") for _ in range(5)),
        )

        names_a = {result["user"]["first_name"] for result in results[:5]}
        names_b = {result["user"]["first_name"] for result in results[5:]}
        assert names_a == {"Alice"}
        assert names_b == {"Bob"}


async def test_two_signed_in_clients_search_the_same_region_without_crossing_over() -> None:
    """Region search is a read, but it still runs per caller and must stay per caller.

    Both clients are authenticated as different people and both ask for the same region at
    the same time. Each has to get its own answer, and each has to reach the platform under
    its own device id -- a shared identity would collapse the two into one.
    """
    async with two_clients() as (client_a, client_b, backend):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        results = await asyncio.gather(
            *(call(client_a, "find_bundles_by_region", region="Europe") for _ in range(3)),
            *(call(client_b, "find_bundles_by_region", region="Europe") for _ in range(3)),
        )

        for result in results:
            assert isinstance(result, dict), f"a region search failed: {result}"
            assert result["status"] == "ok"
            assert [bundle["bundle_code"] for bundle in result["bundles"]] == ["eur-5gb"]

        assert len(set(backend.region_device_ids)) == 2, "two clients presented one device identity"

        # The searches leave the accounts exactly where they were.
        assert (await call(client_a, "get_user_profile"))["user"]["first_name"] == "Alice"
        assert (await call(client_b, "get_user_profile"))["user"]["first_name"] == "Bob"


async def test_region_browsing_needs_no_login_and_leaks_nothing_between_clients() -> None:
    """An anonymous client browsing regions must not be served through a signed-in session."""
    async with two_clients() as (client_a, client_b, backend):
        await sign_in(client_a, "alice@example.com")

        regions = await call(client_b, "list_regions")
        bundles = await call(client_b, "find_bundles_by_region", region="EUR")

        assert regions["status"] == "ok"
        assert bundles["status"] == "ok"
        assert bundles["bundles"], "browsing was blocked behind a login"
        # B browsed successfully and is still nobody.
        assert (await call(client_b, "get_login_status"))["authenticated"] is False
        assert len(set(backend.region_device_ids)) == 1


async def test_a_third_client_starts_unauthenticated_while_two_are_signed_in() -> None:
    """A fresh connection must never land on an existing session."""
    async with one_server() as (url, _), mcp_client(url) as client_a, mcp_client(url) as client_b:
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        async with mcp_client(url) as client_c:
            assert (await call(client_c, "get_login_status"))["authenticated"] is False
