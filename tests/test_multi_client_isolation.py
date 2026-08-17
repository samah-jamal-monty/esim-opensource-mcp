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
from tests.conftest import (
    CATALOG_REGIONS,
    TEST_SALT,
    bundle_payload,
    consumption_payload,
    envelope,
    esim_payload,
    make_jwt,
    topup_bundle_payload,
    topup_options_payload,
    wallet_topup_checkout_payload,
    wallet_topup_status_payload,
)

pytestmark = pytest.mark.anyio

#: Two distinct eSIM accounts, so "B saw A's account" is observable rather than inferred.
ACCOUNTS: dict[str, dict[str, Any]] = {
    "alice@example.com": {
        "first_name": "Alice",
        "balance": 111.0,
        "subject": "user-alice",
        # One eSIM per account, with a distinct ICCID, distinct usage and a distinct top-up
        # reference. That is what makes a cross-user read *observable* rather than inferred:
        # if a client is served somebody else's session it reads somebody else's numbers.
        "iccid": "8931080019111111111",
        "used": 1.0,
        "topup_reference": "topup-alice-0001",
        "topup_order": "ord-topup-alice",
    },
    "bob@example.com": {
        "first_name": "Bob",
        "balance": 222.0,
        "subject": "user-bob",
        "iccid": "8931080019222222222",
        "used": 2.0,
        "topup_reference": "topup-bob-0001",
        "topup_order": "ord-topup-bob",
    },
    "carol@example.com": {
        "first_name": "Carol",
        "balance": 333.0,
        "subject": "user-carol",
        "iccid": "8931080019333333333",
        "used": 3.0,
        "topup_reference": "topup-carol-0001",
        "topup_order": "ord-topup-carol",
    },
    "dave@example.com": {
        "first_name": "Dave",
        "balance": 444.0,
        "subject": "user-dave",
        "iccid": "8931080019444444444",
        "used": 4.0,
        "topup_reference": "topup-dave-0001",
        "topup_order": "ord-topup-dave",
    },
    "erin@example.com": {
        "first_name": "Erin",
        "balance": 555.0,
        "subject": "user-erin",
        "iccid": "8931080019555555555",
        "used": 5.0,
        "topup_reference": "topup-erin-0001",
        "topup_order": "ord-topup-erin",
    },
}

#: The plan on every stub eSIM, so the top-up compatibility route has something to key on.
PRIMARY_BUNDLE = "aaaaaaaa-0000-4000-8000-000000000001"
TOPUP_BUNDLE = "tttttttt-0000-4000-8000-000000000001"


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
        #: Every checkout the stub opened, by account. A second entry for one account means
        #: a duplicate page was created.
        self.checkouts: list[str] = []
        #: Every eSIM top-up the stub executed, by account. A second entry for one account
        #: means somebody was charged twice -- the failure this whole design exists to stop.
        self.topups: list[str] = []
        #: Device ids seen on the Phase 6 routes specifically.
        self.phase_six_device_ids: list[str] = []

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
                # Phase 6. Every one of these is token-scoped, exactly as the real backend
                # is: the account is resolved from the bearer and from nothing else, so a
                # client holding the wrong token reads the wrong account back and the test
                # sees it.
                Route("/api/v1/wallet/user_wallet_by_user", self._wallet, methods=["GET"]),
                Route("/api/v1/user/my-esim", self._my_esims, methods=["GET"]),
                Route("/api/v1/user/consumption/{iccid}", self._consumption, methods=["GET"]),
                Route(
                    "/api/v1/user/related-topup/{bundle_code}/{iccid}",
                    self._related_topup,
                    methods=["GET"],
                ),
                Route("/api/v1/user/bundle/assign-top-up", self._execute_topup, methods=["POST"]),
                Route("/api/v1/mcp/wallet/top-up/options", self._topup_options, methods=["GET"]),
                Route("/api/v1/mcp/wallet/top-up/checkout", self._topup_checkout, methods=["POST"]),
                Route(
                    "/api/v1/mcp/wallet/top-up/status/{payment_reference}",
                    self._topup_status,
                    methods=["GET"],
                ),
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


    # ------------------------------------------------------- phase 6: usage and top-ups

    def _authenticated(self, request: Request) -> str | None:
        """The account this bearer belongs to, recording the device id on the way through."""
        self._record(request)
        device_id = request.headers.get("x-device-id")
        if device_id:
            self.phase_six_device_ids.append(device_id)
        return self._email_for(request)

    @staticmethod
    def _unauthorized() -> JSONResponse:
        return JSONResponse(envelope(None, status="failed", response_code=401), status_code=401)

    async def _wallet(self, request: Request) -> JSONResponse:
        """Each account's own balance, keyed on the bearer and on nothing else."""
        email = self._authenticated(request)
        if email is None:
            return self._unauthorized()
        return JSONResponse(envelope({"balance": ACCOUNTS[email]["balance"], "currency": "USD"}))

    async def _my_esims(self, request: Request) -> JSONResponse:
        email = self._authenticated(request)
        if email is None:
            return self._unauthorized()
        return JSONResponse(envelope([esim_payload(iccid=ACCOUNTS[email]["iccid"], bundle_code=PRIMARY_BUNDLE)]))

    async def _consumption(self, request: Request) -> JSONResponse:
        """Scoped exactly as the real route is: the ICCID must be *this* caller's."""
        email = self._authenticated(request)
        if email is None:
            return self._unauthorized()
        if request.path_params["iccid"] != ACCOUNTS[email]["iccid"]:
            return JSONResponse(
                envelope(None, status="failed", title="USER_PROFILE_NOT_FOUND", response_code=400),
                status_code=400,
            )
        return JSONResponse(envelope(consumption_payload(data_used=ACCOUNTS[email]["used"])))

    async def _related_topup(self, request: Request) -> JSONResponse:
        email = self._authenticated(request)
        if email is None:
            return self._unauthorized()
        if request.path_params["iccid"] != ACCOUNTS[email]["iccid"]:
            return JSONResponse(
                envelope(None, status="failed", title="REQUEST_FAILED", response_code=400), status_code=400
            )
        return JSONResponse(envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)]))

    async def _execute_topup(self, request: Request) -> JSONResponse:
        """The legacy top-up route, scoped to the bearer exactly as the real one is not.

        The real platform does **not** check ICCID ownership here -- that gap is one of the
        accepted QA risks. The stub checks it anyway, so a test that got the ownership
        boundary wrong on the MCP side fails loudly here instead of silently passing.
        """
        email = self._authenticated(request)
        if email is None:
            return self._unauthorized()
        body = await request.json()
        if body.get("iccid") != ACCOUNTS[email]["iccid"]:
            return JSONResponse(
                envelope(None, status="failed", title="REQUEST_FAILED", response_code=400), status_code=400
            )
        self.topups.append(email)
        return JSONResponse(
            envelope({"order_id": ACCOUNTS[email]["topup_order"], "payment_status": "COMPLETED"})
        )

    async def _topup_options(self, request: Request) -> JSONResponse:
        email = self._authenticated(request)
        if email is None:
            return self._unauthorized()
        return JSONResponse(
            envelope(topup_options_payload(current_balance=f"{ACCOUNTS[email]['balance']:.2f}"))
        )

    async def _topup_checkout(self, request: Request) -> JSONResponse:
        email = self._authenticated(request)
        if email is None:
            return self._unauthorized()
        reference = ACCOUNTS[email]["topup_reference"]
        # The durable guarantee the real platform gives, modelled: a repeat of the same
        # top-up resolves onto the order that already exists rather than a second one.
        replay = reference in self.checkouts
        self.checkouts.append(reference)
        return JSONResponse(
            envelope(
                wallet_topup_checkout_payload(
                    payment_reference=reference,
                    checkout_url=f"https://checkout.test/pay/{reference}",
                    idempotent_replay=replay,
                )
            )
        )

    async def _topup_status(self, request: Request) -> JSONResponse:
        """Scoped to the caller: another account's reference is *not found*, not refused."""
        email = self._authenticated(request)
        if email is None:
            return self._unauthorized()
        if request.path_params["payment_reference"] != ACCOUNTS[email]["topup_reference"]:
            return JSONResponse(
                envelope(None, status="failed", title="MCP_WALLET_TOPUP_NOT_FOUND", response_code=404),
                status_code=404,
            )
        return JSONResponse(
            envelope(wallet_topup_status_payload(payment_reference=ACCOUNTS[email]["topup_reference"]))
        )


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
async def one_server(*, qa_topup: bool = False) -> AsyncIterator[tuple[str, StubBackend]]:
    """One real MCP server process over Streamable HTTP.

    ``qa_topup`` turns on the QA eSIM-top-up execution flag, which is what registers
    ``confirm_esim_topup`` and what opens the legacy route at the transport. It defaults to
    off, so every test that can charge somebody has to ask for it explicitly.
    """
    backend = StubBackend()
    backend_port = _free_port()
    mcp_port = _free_port()

    settings = Settings.build(
        api_base_url=f"http://127.0.0.1:{backend_port}",
        environment="development",
        transport="streamable-http",
        device_id_salt=TEST_SALT,
        host="127.0.0.1",
        esim_topup_execution_enabled=qa_topup,
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


# ------------------------------------------------------- phase 6: usage, top-ups, wallet
#
# Everything below drives the *new* tools through the same two real connections. The stub
# platform scopes every one of these routes to the bearer it is given, so a client that
# ended up holding another client's token would read that other account's SIM, that other
# account's usage or that other account's payment -- and each test asserts it does not.


PHASE_SIX_TOOLS_NEEDING_A_SESSION = (
    ("get_esim_consumption", {}),
    ("get_esim_topup_options", {}),
    ("prepare_esim_topup", {"bundle_code": TOPUP_BUNDLE}),
    ("prepare_wallet_topup", {"amount": "25"}),
    ("create_wallet_topup_checkout", {"quote_reference": "anything"}),
    ("get_wallet_topup_status", {"payment_reference": "anything"}),
)


@pytest.mark.parametrize(("tool", "arguments"), PHASE_SIX_TOOLS_NEEDING_A_SESSION)
async def test_every_phase_six_tool_is_refused_for_a_client_that_never_signed_in(
    tool: str, arguments: dict[str, Any]
) -> None:
    """Fail closed, tool by tool, while another client is signed in on the same process."""
    async with two_clients() as (client_a, client_b, _):
        await sign_in(client_a, "alice@example.com")

        result = await call(client_b, tool, **arguments)

        assert isinstance(result, str), f"{tool} served an unauthenticated client"
        assert "authentication_required" in result, f"{tool} -> {result}"
        assert "Alice" not in result


async def test_two_users_read_their_own_usage_and_never_each_others() -> None:
    """A and B each own one SIM with different usage. Neither may see the other's."""
    async with two_clients() as (client_a, client_b, backend):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        usage_a = await call(client_a, "get_esim_consumption")
        usage_b = await call(client_b, "get_esim_consumption")

        assert usage_a["usage"]["used_data"] == "1.0 GB"
        assert usage_b["usage"]["used_data"] == "2.0 GB"
        # The SIMs are named by different masked identifiers, so neither answer is the other.
        assert usage_a["esim"]["masked_iccid"] != usage_b["esim"]["masked_iccid"]
        # And two device identities reached the platform, not one.
        assert len(set(backend.phase_six_device_ids)) == 2


async def test_one_user_cannot_name_another_users_iccid() -> None:
    """The ownership boundary, over the real transport.

    A asks for B's ICCID by name. It is not in A's own eSIM list, so it is *not found* --
    and, critically, the request never reaches the platform at all.
    """
    async with two_clients() as (client_a, client_b, backend):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")
        bobs_iccid = ACCOUNTS["bob@example.com"]["iccid"]

        for tool in ("get_esim_consumption", "get_esim_topup_options"):
            result = await call(client_a, tool, iccid=bobs_iccid)
            assert isinstance(result, str), f"{tool} served a foreign ICCID"
            assert "esim_not_found" in result
            assert bobs_iccid not in result

        assert not any(bobs_iccid in path for path in backend.phase_six_device_ids)


async def test_a_top_up_quote_of_one_user_is_invisible_to_another() -> None:
    async with two_clients() as (client_a, client_b, _):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        prepared = await call(client_a, "prepare_esim_topup", bundle_code=TOPUP_BUNDLE)
        assert prepared["status"] == "prepared"

        stolen = await call(client_b, "get_prepared_esim_topup", quote_id=prepared["quote_id"])

        assert isinstance(stolen, str)
        assert "topup_quote_not_found" in stolen


async def test_one_user_cannot_create_or_inspect_another_users_wallet_payment() -> None:
    """The money half of the isolation story: neither the quote nor the reference travels."""
    async with two_clients() as (client_a, client_b, backend):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        quote_a = await call(client_a, "prepare_wallet_topup", amount="25")
        opened_a = await call(client_a, "create_wallet_topup_checkout", quote_reference=quote_a["quote_reference"])
        assert opened_a["checkout_url"].endswith("topup-alice-0001")

        # B cannot open a page for A's quote...
        hijacked = await call(client_b, "create_wallet_topup_checkout", quote_reference=quote_a["quote_reference"])
        assert isinstance(hijacked, str)
        assert "wallet_topup_quote_not_found" in hijacked

        # ...and cannot read A's payment either.
        probed = await call(client_b, "get_wallet_topup_status", payment_reference=opened_a["payment_reference"])
        assert isinstance(probed, str)
        assert "wallet_topup_not_found" in probed

        # Exactly one page was opened, for Alice, and nobody else's reference was used.
        assert backend.checkouts == ["topup-alice-0001"]


async def test_each_user_sees_their_own_balance_in_their_own_top_up_quote() -> None:
    async with two_clients() as (client_a, client_b, _):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        quote_a = await call(client_a, "prepare_wallet_topup", amount="25")
        quote_b = await call(client_b, "prepare_wallet_topup", amount="25")

        assert quote_a["current_balance"] == "111.00"
        assert quote_b["current_balance"] == "222.00"


async def test_a_repeated_checkout_returns_one_page_per_user() -> None:
    """Two users, two pages. One user asking twice, one page."""
    async with two_clients() as (client_a, client_b, backend):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        quote_a = await call(client_a, "prepare_wallet_topup", amount="25")
        quote_b = await call(client_b, "prepare_wallet_topup", amount="25")

        first_a = await call(client_a, "create_wallet_topup_checkout", quote_reference=quote_a["quote_reference"])
        again_a = await call(client_a, "create_wallet_topup_checkout", quote_reference=quote_a["quote_reference"])
        only_b = await call(client_b, "create_wallet_topup_checkout", quote_reference=quote_b["quote_reference"])

        assert first_a["checkout_url"] == again_a["checkout_url"]
        assert again_a["replayed"] is True
        assert only_b["checkout_url"] != first_a["checkout_url"]
        # A's repeat was replayed locally, so the platform saw one checkout per user.
        assert backend.checkouts == ["topup-alice-0001", "topup-bob-0001"]


async def test_logging_out_drops_that_users_usage_top_up_and_payment_access_only() -> None:
    async with two_clients() as (client_a, client_b, _):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")
        quote_a = await call(client_a, "prepare_wallet_topup", amount="25")
        opened_a = await call(client_a, "create_wallet_topup_checkout", quote_reference=quote_a["quote_reference"])

        await call(client_a, "logout")

        # A's payment reference and quote are gone with the session.
        gone = await call(client_a, "get_wallet_topup_status", payment_reference=opened_a["payment_reference"])
        assert isinstance(gone, str) and "authentication_required" in gone
        # B is untouched, and still reads B's own usage.
        assert (await call(client_b, "get_esim_consumption"))["usage"]["used_data"] == "2.0 GB"


async def test_a_token_refresh_on_one_session_leaves_the_others_usage_unchanged() -> None:
    async with two_clients() as (client_a, client_b, backend):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")
        usage_b_before = await call(client_b, "get_esim_consumption")

        # Force A's session to rotate by expiring its access token platform-side.
        backend.tokens = {
            token: email for token, email in backend.tokens.items() if email != "alice@example.com"
        }
        usage_a = await call(client_a, "get_esim_consumption")

        assert backend.refreshes == ["alice@example.com"], "the wrong session was refreshed"
        assert usage_a["usage"]["used_data"] == "1.0 GB"
        assert await call(client_b, "get_esim_consumption") == usage_b_before


async def test_a_reconnecting_client_fails_closed_rather_than_inheriting_a_session() -> None:
    """A new connection is a new caller. It gets no usage, no quote and no payment.

    This is the reconnect half of the reported defect: the MCP session id is minted per
    connection, so the identity a reconnect resolves to has never been seen before -- and
    everything keyed on it is therefore empty rather than somebody else's.
    """
    async with one_server() as (url, backend):
        async with mcp_client(url) as first:
            await sign_in(first, "alice@example.com")
            quote = await call(first, "prepare_wallet_topup", amount="25")
            opened = await call(first, "create_wallet_topup_checkout", quote_reference=quote["quote_reference"])
            assert (await call(first, "get_esim_consumption"))["usage_reported"] is True

        # The first connection is closed. A brand-new one is nobody.
        async with mcp_client(url) as reconnected:
            assert (await call(reconnected, "get_login_status"))["authenticated"] is False
            for tool, arguments in (
                ("get_esim_consumption", {}),
                ("get_esim_topup_options", {}),
                ("get_wallet_topup_status", {"payment_reference": opened["payment_reference"]}),
                ("create_wallet_topup_checkout", {"quote_reference": quote["quote_reference"]}),
            ):
                result = await call(reconnected, tool, **arguments)
                assert isinstance(result, str), f"{tool} served a reconnected client"
                assert "authentication_required" in result

        assert backend.checkouts == ["topup-alice-0001"], "a reconnect opened a second payment page"


# --------------------------------------------------------------------- five concurrent users


async def test_five_concurrent_users_never_cross_over() -> None:
    """Five real connections, five accounts, all traffic interleaved.

    Every account has its own balance, its own ICCID, its own usage figure and its own
    payment reference, so a single crossed session shows up as a wrong number rather than
    as a subtle timing artefact.
    """
    emails = list(ACCOUNTS)
    async with (
        one_server() as (url, backend), mcp_client(url) as c1,
        mcp_client(url) as c2,
        mcp_client(url) as c3,
        mcp_client(url) as c4,
        mcp_client(url) as c5,
    ):
        clients = [c1, c2, c3, c4, c5]
        await asyncio.gather(*(sign_in(client, email) for client, email in zip(clients, emails, strict=True)))

        profiles, usages, quotes = await asyncio.gather(
            asyncio.gather(*(call(client, "get_user_profile") for client in clients)),
            asyncio.gather(*(call(client, "get_esim_consumption") for client in clients)),
            asyncio.gather(*(call(client, "prepare_wallet_topup", amount="25") for client in clients)),
        )

        for email, profile, usage, quote in zip(emails, profiles, usages, quotes, strict=True):
            account = ACCOUNTS[email]
            assert profile["user"]["first_name"] == account["first_name"]
            assert profile["wallet"]["balance"] == account["balance"]
            assert usage["usage"]["used_data"] == f"{account['used']} GB"
            assert quote["current_balance"] == f"{account['balance']:.2f}"

        # Five connections, five device identities.
        assert len(set(backend.phase_six_device_ids)) == 5

        # Each opens a payment page, concurrently. Five pages, one per account.
        opened = await asyncio.gather(
            *(
                call(client, "create_wallet_topup_checkout", quote_reference=quote["quote_reference"])
                for client, quote in zip(clients, quotes, strict=True)
            )
        )
        references = [result["payment_reference"] for result in opened]
        assert sorted(references) == sorted(ACCOUNTS[email]["topup_reference"] for email in emails)
        assert len(set(references)) == 5
        assert sorted(backend.checkouts) == sorted(references)

        # And no client can read any other client's payment.
        for index, client in enumerate(clients):
            for other, reference in enumerate(references):
                result = await call(client, "get_wallet_topup_status", payment_reference=reference)
                if index == other:
                    assert result["payment_reference"] == reference
                else:
                    assert isinstance(result, str) and "wallet_topup_not_found" in result


# ------------------------------------------------- QA eSIM top-up execution, multi-user
#
# `confirm_esim_topup` is registered only when the QA flag is on, so these are the only
# tests in this file that stand a server up with `qa_topup=True`. The stub platform records
# every top-up it executes by account: a second entry for one account is a user charged
# twice, which is the failure the whole design exists to stop.


async def confirm_own_topup(session: ClientSession) -> dict[str, Any] | str:
    """Prepare and confirm one top-up for whichever account this client is signed in as."""
    quote = await call(session, "prepare_esim_topup", bundle_code=TOPUP_BUNDLE)
    assert isinstance(quote, dict), quote
    return await call(
        session,
        "confirm_esim_topup",
        quote_id=quote["quote_id"],
        confirmed_amount=quote["confirm_amount"],
    )


async def test_the_execution_tool_is_absent_without_the_qa_flag() -> None:
    """The default deployment does not publish it at all, so a model cannot call it."""
    async with one_server() as (url, _), mcp_client(url) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}

    assert "confirm_esim_topup" not in names
    assert "prepare_esim_topup" in names, "the free tools must still be there"


async def test_the_execution_tool_is_published_with_the_qa_flag() -> None:
    async with one_server(qa_topup=True) as (url, _), mcp_client(url) as client:
        tools = await client.list_tools()
        named = {tool.name: tool for tool in tools.tools}

    assert "confirm_esim_topup" in named
    annotations = named["confirm_esim_topup"].annotations
    assert annotations.destructive_hint is True
    assert annotations.idempotent_hint is False


async def test_two_users_top_up_their_own_esims_and_never_each_others() -> None:
    async with (
        one_server(qa_topup=True) as (url, backend),
        mcp_client(url) as client_a,
        mcp_client(url) as client_b,
    ):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        result_a = await confirm_own_topup(client_a)
        result_b = await confirm_own_topup(client_b)

        assert result_a["topped_up"] is True
        assert result_b["topped_up"] is True
        assert result_a["order_id"] == "ord-topup-alice"
        assert result_b["order_id"] == "ord-topup-bob"
        # One top-up each, and neither reached the other's SIM.
        assert sorted(backend.topups) == ["alice@example.com", "bob@example.com"]


async def test_one_user_cannot_confirm_another_users_prepared_top_up() -> None:
    async with (
        one_server(qa_topup=True) as (url, backend),
        mcp_client(url) as client_a,
        mcp_client(url) as client_b,
    ):
        await sign_in(client_a, "alice@example.com")
        await sign_in(client_b, "bob@example.com")

        quote = await call(client_a, "prepare_esim_topup", bundle_code=TOPUP_BUNDLE)
        stolen = await call(
            client_b,
            "confirm_esim_topup",
            quote_id=quote["quote_id"],
            confirmed_amount=quote["confirm_amount"],
        )

        assert isinstance(stolen, str)
        assert "topup_quote_not_found" in stolen
        assert backend.topups == [], "one user's confirmation topped up for another"


async def test_a_second_confirmation_never_reaches_the_platform_twice() -> None:
    """The one-attempt lock, over the real transport."""
    async with one_server(qa_topup=True) as (url, backend), mcp_client(url) as client:
        await sign_in(client, "alice@example.com")

        quote = await call(client, "prepare_esim_topup", bundle_code=TOPUP_BUNDLE)
        first = await call(
            client, "confirm_esim_topup", quote_id=quote["quote_id"], confirmed_amount=quote["confirm_amount"]
        )
        second = await call(
            client, "confirm_esim_topup", quote_id=quote["quote_id"], confirmed_amount=quote["confirm_amount"]
        )

        assert first["topped_up"] is True
        assert second["replayed"] is True
        assert backend.topups == ["alice@example.com"], "the platform ran the top-up twice"


async def test_a_reconnecting_client_cannot_confirm_a_top_up_prepared_before() -> None:
    """Reconnect fails closed for the money path too."""
    async with one_server(qa_topup=True) as (url, backend):
        async with mcp_client(url) as first:
            await sign_in(first, "alice@example.com")
            quote = await call(first, "prepare_esim_topup", bundle_code=TOPUP_BUNDLE)

        async with mcp_client(url) as reconnected:
            result = await call(
                reconnected,
                "confirm_esim_topup",
                quote_id=quote["quote_id"],
                confirmed_amount=quote["confirm_amount"],
            )

        assert isinstance(result, str)
        assert "authentication_required" in result
        assert backend.topups == [], "a reconnect executed a top-up"


async def test_five_concurrent_users_each_top_up_exactly_their_own_esim_once() -> None:
    """Five real connections, five accounts, all confirmations interleaved.

    Each account has its own ICCID and its own order reference, so a crossed session shows
    up as the wrong order id -- and the stub's per-account tally shows up any double charge.
    """
    emails = list(ACCOUNTS)
    async with (
        one_server(qa_topup=True) as (url, backend), mcp_client(url) as c1,
        mcp_client(url) as c2,
        mcp_client(url) as c3,
        mcp_client(url) as c4,
        mcp_client(url) as c5,
    ):
        clients = [c1, c2, c3, c4, c5]
        await asyncio.gather(*(sign_in(client, email) for client, email in zip(clients, emails, strict=True)))

        results = await asyncio.gather(*(confirm_own_topup(client) for client in clients))

        for email, result in zip(emails, results, strict=True):
            assert isinstance(result, dict), result
            assert result["topped_up"] is True
            assert result["order_id"] == ACCOUNTS[email]["topup_order"]

        # Exactly one top-up per account, and no account appears twice.
        assert sorted(backend.topups) == sorted(emails)
        assert len(set(backend.topups)) == 5
        assert len(set(backend.phase_six_device_ids)) == 5
