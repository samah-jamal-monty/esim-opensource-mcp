"""The account-history read budget, and the retry behaviour that goes with it.

The bug this file pins down was two mistakes compounding. ``GET /user/my-esim`` is built by
the platform per user and per request rather than served from a cache, so on a real account it
answered later than the *general* read budget (20s) allowed -- and that budget was the one
applied to it. The read then went through the shared three-attempt read retry, so a single
``get_my_esims`` spent 3 x 20s failing, the assistant tried the tool a second time, and the
user waited about two and a half minutes to be told nothing at all.

So there are two independent properties here, and neither is sufficient alone:

* the two account reads get :attr:`~esim_mcp.settings.Settings.account_read_timeout`, which is
  wide enough for what the platform actually takes, and *only* those two reads get it;
* one tool call sends exactly one request. A widened budget with the shared retry still on it
  would be strictly worse than the bug -- six minutes of silence instead of one.

And one rule that is really about honesty rather than timing: a read that ran out of budget is
not an account with nothing in it, and it is not an expired token either. It raises its own
typed error, it does not rotate the token, and it does not replay. The one place a read *is*
replayed is a real ``401``, exactly once, which is asserted here too so that the exception
cannot quietly widen.

Nothing here buys, tops up, provisions or cancels anything: only the two read-only account
routes, one catalogue route and the login routes are mocked, so any other call would surface
as an unmocked request rather than pass silently.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from esim_mcp.client.account import AccountApiClient
from esim_mcp.client.auth import AuthApiClient
from esim_mcp.client.base import BackendApiClient
from esim_mcp.client.catalog import CatalogApiClient
from esim_mcp.errors import (
    AccountReadTimeoutError,
    AuthenticationRequiredError,
    BackendTimeoutError,
    BackendUnavailableError,
)
from esim_mcp.selection.service import EsimSelectionService
from esim_mcp.selection.store import InMemoryEsimSelectionStore
from esim_mcp.session.identity import ClientIdentityProvider
from esim_mcp.session.manager import SessionManager
from esim_mcp.session.store import InMemorySessionStore
from esim_mcp.settings import Settings
from esim_mcp.tools.account import AccountService
from esim_mcp.tools.authentication import AuthenticationService
from esim_mcp.tools.catalog import CatalogService
from tests.conftest import (
    API_URL,
    BASE_URL,
    TEST_SALT,
    StubIdentityProvider,
    auth_payload,
    country_payload,
    envelope,
    esim_payload,
    make_jwt,
)

MY_ESIM_URL = f"{API_URL}/user/my-esim"
ORDER_HISTORY_URL = f"{API_URL}/user/order-history"
REFRESH_URL = f"{API_URL}/auth/refresh-token"
COUNTRIES_URL = f"{API_URL}/bundles/countries"


def order_payload() -> dict[str, Any]:
    """One past order, trimmed to the fields this file cares about."""
    return {
        "order_number": "ord-0001",
        "order_status": "SUCCESS",
        "order_amount": 8.06,
        "order_currency": "USD",
        "order_display_price": "8.06 USD",
        "order_date": "1754472554",
        "order_type": "Assign",
        "quantity": 1,
        "payment_type": "Card",
    }


def failure_response(status: int, *, title: str) -> httpx.Response:
    return httpx.Response(status, json=envelope(None, status="failed", title=title, response_code=status))


def esims_response(label: str = "Paris trip") -> httpx.Response:
    return httpx.Response(200, json=envelope([esim_payload(label=label)]))


# --------------------------------------------------------------------------------- harness


class Harness:
    """One shared transport and session store, and as many callers as a test asks for.

    Sharing is the point for the isolation tests: "one user's timeout does not touch another"
    only means something when both users read through the same transport, the same pooled
    connections and the same session store.
    """

    def __init__(self, settings: Settings, router: respx.Router, client: BackendApiClient) -> None:
        self.settings = settings
        self.router = router
        self.client = client
        self._auth_client = AuthApiClient(client)
        self._account_client = AccountApiClient(client)
        self._catalog_client = CatalogApiClient(client)
        self.sessions = SessionManager(settings, InMemorySessionStore(), self._auth_client)
        self._selection = EsimSelectionService(InMemoryEsimSelectionStore())
        self.sessions.add_invalidation_listener(self._selection.invalidate_session)
        self._pending_login: dict[str, Any] = {}

        self.login = router.post(f"{API_URL}/auth/login").mock(
            return_value=httpx.Response(200, json=envelope(None))
        )
        self.verify = router.post(f"{API_URL}/auth/verify_otp").mock(side_effect=self._verify)
        self.esims = router.get(MY_ESIM_URL).mock(return_value=esims_response())
        self.orders = router.get(ORDER_HISTORY_URL).mock(
            return_value=httpx.Response(200, json=envelope([order_payload()]))
        )
        self.countries = router.get(COUNTRIES_URL).mock(
            return_value=httpx.Response(200, json=envelope([country_payload()]))
        )
        # Stubbed as a failure by default so no test can pass by accidentally rotating a
        # token: a successful refresh has to be opted into explicitly, which makes every
        # replay assertion below about a refresh the test itself asked for.
        self.refresh = router.post(REFRESH_URL).mock(return_value=failure_response(401, title="TOKEN_EXPIRED"))

    def _verify(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(auth_payload(**self._pending_login)))

    # ------------------------------------------------------------------ callers

    def identity(self, name: str) -> StubIdentityProvider:
        return StubIdentityProvider(name)

    def accounts(self, identity: ClientIdentityProvider) -> AccountService:
        return AccountService(self.settings, self._account_client, self.sessions, identity, self._selection)

    def auth(self, identity: ClientIdentityProvider) -> AuthenticationService:
        return AuthenticationService(self.settings, self._auth_client, self.sessions, identity)

    def catalog(self, identity: ClientIdentityProvider) -> CatalogService:
        return CatalogService(self.settings, self._catalog_client, identity)

    async def sign_in(self, identity: StubIdentityProvider, *, email: str, access_token: str) -> str:
        """Drive a real OTP login and return the bearer token that session now holds."""
        self._pending_login = {"access_token": access_token, "email": email}
        service = self.auth(identity)
        await service.request_login_otp(email=email)
        await service.verify_login_otp(verification_pin="123456", email=email)
        return access_token

    # ------------------------------------------------------------------- calls

    def calls_to(self, fragment: str) -> list[httpx.Request]:
        return [call.request for call in self.router.calls if fragment in call.request.url.path]

    @property
    def esim_calls(self) -> list[httpx.Request]:
        return self.calls_to("/user/my-esim")

    @property
    def order_calls(self) -> list[httpx.Request]:
        return self.calls_to("/user/order-history")

    @property
    def refresh_calls(self) -> list[httpx.Request]:
        return self.calls_to("/auth/refresh-token")

    @staticmethod
    def bearer(request: httpx.Request) -> str:
        return request.headers["Authorization"].removeprefix("Bearer ")


@pytest.fixture
async def harness(settings: Settings, respx_mock: respx.Router) -> AsyncIterator[Harness]:
    client = BackendApiClient(settings)
    try:
        yield Harness(settings, respx_mock, client)
    finally:
        await client.aclose()


@pytest.fixture
async def user(harness: Harness) -> tuple[StubIdentityProvider, str]:
    identity = harness.identity("client-a")
    token = await harness.sign_in(identity, email="a@example.com", access_token=make_jwt(subject="user-a"))
    return identity, token


# --------------------------------------------------------------------------- the budget


async def test_the_two_account_reads_get_their_own_wider_budget(
    harness: Harness, user: tuple[StubIdentityProvider, str]
) -> None:
    """Both routes, on the account budget rather than the general one."""
    identity, _ = user
    accounts = harness.accounts(identity)

    await accounts.get_my_esims()
    await accounts.get_order_history()

    for request in (harness.esim_calls[0], harness.order_calls[0]):
        assert request.extensions["timeout"]["read"] == harness.settings.account_read_timeout

    assert harness.settings.account_read_timeout > harness.settings.read_timeout, (
        "the account budget must exceed the one sized for single cached reads"
    )


async def test_the_default_account_budget_clears_the_measured_backend_latency(settings: Settings) -> None:
    """120s: chosen to be well clear of the measured latency rather than tight against it."""
    assert settings.account_read_timeout == 120.0


async def test_widening_the_account_budget_moves_no_other_budget(settings: Settings) -> None:
    """The whole point of a per-route budget: it is per-route."""
    widened = Settings.build(
        api_base_url=BASE_URL,
        environment="qa",
        device_id_salt=TEST_SALT,
        account_read_timeout=300.0,
    )

    assert widened.account_read_timeout == 300.0
    assert widened.read_timeout == settings.read_timeout == 20.0
    assert widened.checkout_read_timeout == settings.checkout_read_timeout == 45.0
    assert widened.purchase_read_timeout == settings.purchase_read_timeout == 90.0
    assert widened.connect_timeout == settings.connect_timeout
    assert widened.write_timeout == settings.write_timeout
    assert widened.pool_timeout == settings.pool_timeout


async def test_the_account_budget_is_configurable_from_its_own_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deployments tune this without touching, or being able to touch, anything else."""
    monkeypatch.setenv("ESIM_API_BASE_URL", BASE_URL)
    monkeypatch.setenv("ESIM_MCP_ENVIRONMENT", "qa")
    monkeypatch.setenv("ESIM_MCP_DEVICE_ID_SALT", TEST_SALT)
    monkeypatch.setenv("ESIM_MCP_ACCOUNT_READ_TIMEOUT", "150")

    configured = Settings.build()

    assert configured.account_read_timeout == 150.0
    assert configured.read_timeout == 20.0
    assert configured.purchase_read_timeout == 90.0


async def test_a_slow_account_read_still_answers_inside_the_account_budget(
    harness: Harness, user: tuple[StubIdentityProvider, str]
) -> None:
    """The boundary the bug lived at: an answer arriving after the *general* budget.

    Before the fix this raised a timeout and the user was told nothing; the eSIMs were there
    the whole time, exactly as they are on the portal.
    """
    identity, _ = user

    async def slow_but_successful(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] > harness.settings.read_timeout
        await asyncio.sleep(0)  # the await point a real slow response would suspend at
        return esims_response()

    harness.esims.mock(side_effect=slow_but_successful)

    result = await harness.accounts(identity).get_my_esims()

    assert result["status"] == "ok"
    assert result["total_count"] == 1


async def test_a_response_slower_than_the_general_budget_really_does_arrive() -> None:
    """The same property against a real socket, where httpx enforces the budget itself.

    respx answers inside the mock transport, so a mocked test can prove which budget was
    *asked for* but never that httpx honours it. This one starts an actual server that stalls
    for longer than the general budget and answers inside the account budget: the read
    succeeds, which it could not do if the general budget were still the one in force.
    """
    delay = 0.4

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            await asyncio.sleep(delay)
            body = httpx.Response(200, json=envelope([esim_payload()])).content
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    slow_settings = Settings.build(
        api_base_url=f"http://127.0.0.1:{port}",
        environment="qa",
        device_id_salt=TEST_SALT,
        read_timeout=0.15,
        account_read_timeout=10.0,
    )
    client = BackendApiClient(slow_settings)
    accounts = AccountApiClient(client)
    try:
        async with server:
            data = await accounts.get_my_esims(
                device_id="device-a",
                access_token=SecretStr("token-a"),
                locale="en",
                currency="USD",
            )
    finally:
        await client.aclose()

    assert isinstance(data, list)
    assert len(data) == 1


async def test_the_general_budget_really_would_have_timed_this_out() -> None:
    """The control for the test above: the same stall, on the general budget, fails.

    Without this the previous test proves only that a 0.4s response arrives, not that the
    account budget is what let it.
    """
    delay = 0.4

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            await asyncio.sleep(delay)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    tight = Settings.build(
        api_base_url=f"http://127.0.0.1:{port}",
        environment="qa",
        device_id_salt=TEST_SALT,
        read_timeout=0.15,
    )
    client = BackendApiClient(tight)
    catalog = CatalogApiClient(client)
    try:
        async with server:
            with pytest.raises(BackendTimeoutError):
                await catalog.list_countries(device_id="device-a", locale="en")
    finally:
        await client.aclose()


# ---------------------------------------------------------------------- one call, no retry


async def test_an_ordinary_timeout_makes_exactly_one_backend_call(
    harness: Harness, user: tuple[StubIdentityProvider, str]
) -> None:
    """The retry half of the bug. One tool call, one request, whatever the outcome."""
    identity, _ = user
    harness.esims.mock(side_effect=httpx.ReadTimeout("slower than the budget"))

    with pytest.raises(AccountReadTimeoutError):
        await harness.accounts(identity).get_my_esims()

    assert len(harness.esim_calls) == 1, "the read was retried after an ordinary timeout"
    # Two login calls and the one read. Nothing else went anywhere near the backend.
    assert len(harness.router.calls) == 3


async def test_an_ordinary_timeout_does_not_refresh_the_token(
    harness: Harness, user: tuple[StubIdentityProvider, str]
) -> None:
    """A slow platform is not an expired token, and must never be handled as one."""
    identity, token = user
    harness.esims.mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(AccountReadTimeoutError):
        await harness.accounts(identity).get_my_esims()

    assert not harness.refresh_calls, "a timeout was treated as an authentication failure"
    session = await harness.sessions.get_session(identity.identity.session_key)
    assert session is not None, "a timeout invalidated the session"
    assert session.access_token.get_secret_value() == token, "a timeout rotated the bearer token"


async def test_a_timeout_is_a_typed_read_timeout_and_never_an_empty_account(
    harness: Harness, user: tuple[StubIdentityProvider, str]
) -> None:
    """The failure mode that matters: "slow" must not reach a user as "you own nothing"."""
    identity, _ = user
    harness.esims.mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(AccountReadTimeoutError) as excinfo:
        await harness.accounts(identity).get_my_esims()

    error = excinfo.value
    assert error.code == "account_read_timeout"
    assert isinstance(error, BackendTimeoutError), "existing timeout handling must still catch this"
    message = error.message.lower()
    assert "not an empty account" in message
    assert "never tell the user they have no esims" in message
    assert "do not call the tool again by yourself" in message
    # And nothing about it looks like a successful, empty answer.
    assert "total_count" not in error.to_dict()
    assert error.to_dict()["status"] == "error"


async def test_a_transport_failure_is_still_the_generic_unavailable_error(
    harness: Harness, user: tuple[StubIdentityProvider, str]
) -> None:
    """Only a *timeout* gets the new type. A refused connection is a different fact."""
    identity, _ = user
    harness.esims.mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(BackendUnavailableError) as excinfo:
        await harness.accounts(identity).get_my_esims()

    assert not isinstance(excinfo.value, AccountReadTimeoutError)
    assert len(harness.esim_calls) == 1


async def test_the_tool_descriptions_tell_the_model_not_to_retry_a_timeout(settings: Settings) -> None:
    """The model is what called the tool a second time, so it is told not to, in words."""
    from esim_mcp.server import build_components

    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        described = {tool.name: tool.description.lower() for tool in await components.server.list_tools()}
    finally:
        await components.aclose()

    for name in ("get_my_esims", "get_order_history"):
        description = described[name]
        assert "account_read_timeout" in description, name
        assert "do not call this tool again by yourself" in description, name
    assert "not an empty account" in described["get_my_esims"]
    assert "not an account without orders" in described["get_order_history"]


# ------------------------------------------------------------------ the one allowed replay


async def test_a_401_refreshes_once_and_replays_exactly_once(
    harness: Harness, user: tuple[StubIdentityProvider, str]
) -> None:
    """The single sanctioned exception to "one request per tool call"."""
    identity, _ = user
    rotated = make_jwt(subject="user-a-rotated")
    harness.refresh.mock(return_value=httpx.Response(200, json=envelope(auth_payload(access_token=rotated))))
    harness.esims.mock(side_effect=[failure_response(401, title="TOKEN_EXPIRED"), esims_response()])

    result = await harness.accounts(identity).get_my_esims()

    assert result["total_count"] == 1
    assert len(harness.esim_calls) == 2, "the read was not replayed exactly once"
    assert len(harness.refresh_calls) == 1, "more than one refresh was attempted"
    assert harness.bearer(harness.esim_calls[1]) == rotated, "the replay did not use the rotated token"


async def test_a_second_401_fails_without_another_replay(
    harness: Harness, user: tuple[StubIdentityProvider, str]
) -> None:
    """Refresh, replay, stop. A second rejection ends the session rather than looping."""
    identity, _ = user
    harness.refresh.mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=make_jwt(subject="rotated"))))
    )
    harness.esims.mock(return_value=failure_response(401, title="TOKEN_EXPIRED"))

    with pytest.raises(AuthenticationRequiredError):
        await harness.accounts(identity).get_my_esims()

    assert len(harness.esim_calls) == 2, "the read was attempted more than twice"
    assert len(harness.refresh_calls) == 1, "a second refresh was attempted"
    assert await harness.sessions.get_session(identity.identity.session_key) is None


async def test_a_timeout_during_the_replay_stops_there(
    harness: Harness, user: tuple[StubIdentityProvider, str]
) -> None:
    """The replay is one request too. A slow platform on the second try is not a third try."""
    identity, _ = user
    harness.refresh.mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=make_jwt(subject="rotated"))))
    )
    harness.esims.mock(side_effect=[failure_response(401, title="TOKEN_EXPIRED"), httpx.ReadTimeout("slow")])

    with pytest.raises(AccountReadTimeoutError):
        await harness.accounts(identity).get_my_esims()

    assert len(harness.esim_calls) == 2
    assert len(harness.refresh_calls) == 1


# -------------------------------------------------------------------------- order history


async def test_an_order_history_timeout_behaves_exactly_as_the_esim_read_does(
    harness: Harness, user: tuple[StubIdentityProvider, str]
) -> None:
    """One request, the typed error, no refresh, and never "you have never ordered anything"."""
    identity, token = user
    harness.orders.mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(AccountReadTimeoutError) as excinfo:
        await harness.accounts(identity).get_order_history()

    assert excinfo.value.code == "account_read_timeout"
    assert len(harness.order_calls) == 1, "the order read was retried after an ordinary timeout"
    assert not harness.refresh_calls
    session = await harness.sessions.get_session(identity.identity.session_key)
    assert session is not None and session.access_token.get_secret_value() == token


async def test_an_order_history_401_refreshes_once_and_replays_once(
    harness: Harness, user: tuple[StubIdentityProvider, str]
) -> None:
    identity, _ = user
    harness.refresh.mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=make_jwt(subject="rotated"))))
    )
    harness.orders.mock(
        side_effect=[
            failure_response(401, title="TOKEN_EXPIRED"),
            httpx.Response(200, json=envelope([order_payload()])),
        ]
    )

    result = await harness.accounts(identity).get_order_history()

    assert result["returned_count"] == 1
    assert len(harness.order_calls) == 2
    assert len(harness.refresh_calls) == 1


# -------------------------------------------------------------------- multi-user isolation


async def test_two_users_stay_isolated_when_one_of_them_times_out(harness: Harness) -> None:
    """One user's slow read is one user's problem. The other reads normally, on her own token."""
    identity_a = harness.identity("client-a")
    identity_b = harness.identity("client-b")
    token_a = await harness.sign_in(identity_a, email="a@example.com", access_token=make_jwt(subject="user-a"))
    token_b = await harness.sign_in(identity_b, email="b@example.com", access_token=make_jwt(subject="user-b"))

    async def by_caller(request: httpx.Request) -> httpx.Response:
        if harness.bearer(request) == token_a:
            raise httpx.ReadTimeout("slow for a only")
        return esims_response(label="B's SIM")

    harness.esims.mock(side_effect=by_caller)

    with pytest.raises(AccountReadTimeoutError):
        await harness.accounts(identity_a).get_my_esims()
    result_b = await harness.accounts(identity_b).get_my_esims()

    assert result_b["esims"][0]["label"] == "B's SIM"
    assert len(harness.esim_calls) == 2, "one user's timeout produced traffic for the other"
    assert harness.bearer(harness.esim_calls[0]) == token_a
    assert harness.bearer(harness.esim_calls[1]) == token_b
    assert len({request.headers["X-Device-Id"] for request in harness.esim_calls}) == 2, (
        "two MCP clients shared one device identity"
    )
    # A's session survives its own timeout, and B's was never touched by it.
    assert await harness.sessions.get_session(identity_a.identity.session_key) is not None
    assert await harness.sessions.get_session(identity_b.identity.session_key) is not None


async def test_five_concurrent_users_each_get_their_own_answer(harness: Harness) -> None:
    """Five callers in flight at once over one transport, one of them timing out.

    The properties that would matter if they regressed: each request carries its owner's exact
    bearer and its own derived device id, each answer goes back to the caller that asked for
    it, and the one timeout neither leaks into another caller's result nor costs anybody a
    second request.
    """
    users: list[tuple[StubIdentityProvider, str]] = []
    for index in range(5):
        identity = harness.identity(f"client-{index}")
        token = await harness.sign_in(
            identity, email=f"u{index}@example.com", access_token=make_jwt(subject=f"user-{index}")
        )
        users.append((identity, token))

    slow_token = users[2][1]
    label_of = {token: f"SIM for {identity.identity.value}" for identity, token in users}

    async def by_caller(request: httpx.Request) -> httpx.Response:
        bearer = harness.bearer(request)
        if bearer == slow_token:
            raise httpx.ReadTimeout("slow for one caller")
        await asyncio.sleep(0)  # force interleaving between the five in-flight reads
        return esims_response(label=label_of[bearer])

    harness.esims.mock(side_effect=by_caller)

    results = await asyncio.gather(
        *(harness.accounts(identity).get_my_esims() for identity, _ in users),
        return_exceptions=True,
    )

    for index, ((_, token), result) in enumerate(zip(users, results, strict=True)):
        if index == 2:
            assert isinstance(result, AccountReadTimeoutError)
            continue
        assert not isinstance(result, BaseException), result
        assert result["esims"][0]["label"] == label_of[token]

    assert len(harness.esim_calls) == 5, "five callers did not produce exactly five reads"
    assert len({harness.bearer(request) for request in harness.esim_calls}) == 5
    assert len({request.headers["X-Device-Id"] for request in harness.esim_calls}) == 5
    assert not harness.refresh_calls
    for identity, _ in users:
        assert await harness.sessions.get_session(identity.identity.session_key) is not None


async def test_every_account_read_carries_the_callers_exact_bearer_token(harness: Harness) -> None:
    """The authenticated token and the per-client device id survive the new path unaltered."""
    identity = harness.identity("client-a")
    token = await harness.sign_in(identity, email="a@example.com", access_token=make_jwt(subject="user-a"))

    accounts = harness.accounts(identity)
    await accounts.get_my_esims()
    await accounts.get_order_history()

    for request in (harness.esim_calls[0], harness.order_calls[0]):
        assert request.headers["Authorization"] == f"Bearer {token}"
        assert request.headers["X-Device-Id"]
        assert "X-Refresh-Token" not in request.headers


async def test_an_unauthenticated_caller_still_reaches_no_backend_route(harness: Harness) -> None:
    """Fail-closed: no session, no request, whatever the budget is."""
    nobody = harness.identity("nobody")

    with pytest.raises(AuthenticationRequiredError):
        await harness.accounts(nobody).get_my_esims()
    with pytest.raises(AuthenticationRequiredError):
        await harness.accounts(nobody).get_order_history()

    assert not harness.esim_calls
    assert not harness.order_calls


# --------------------------------------------------------- every other budget is unchanged


async def test_the_general_read_budget_still_governs_a_catalogue_read(harness: Harness) -> None:
    """The account budget is not global: an ordinary read is still on the ordinary one."""
    await harness.catalog(harness.identity("client-a")).list_countries()

    read_budget = harness.calls_to("/bundles/countries")[0].extensions["timeout"]["read"]
    assert read_budget == harness.settings.read_timeout
    assert read_budget != harness.settings.account_read_timeout


async def test_a_catalogue_read_still_uses_the_shared_retry_policy(harness: Harness) -> None:
    """Only the two account reads dropped to one attempt. Nothing else moved."""
    harness.countries.mock(
        side_effect=[
            httpx.Response(503, json=envelope(None, status="failed", response_code=503)),
            httpx.Response(200, json=envelope([country_payload()])),
        ]
    )

    await harness.catalog(harness.identity("client-a")).list_countries()

    assert len(harness.calls_to("/bundles/countries")) == 2, "the shared read retry was removed from other reads"


async def test_the_purchase_and_checkout_budgets_are_untouched(settings: Settings) -> None:
    """The two payment budgets are unrelated to this change and must stay where they were."""
    assert settings.checkout_read_timeout == 45.0
    assert settings.purchase_read_timeout == 90.0
    assert settings.read_timeout == 20.0
    assert settings.write_timeout == 20.0
    assert settings.connect_timeout == 5.0
    assert settings.pool_timeout == 5.0


def test_only_the_account_read_path_reads_the_account_budget() -> None:
    """Asserted against the source, so a future edit cannot quietly borrow this budget."""
    source_root = Path(__file__).resolve().parents[1] / "src" / "esim_mcp"
    readers = {
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if ".account_read_timeout" in path.read_text(encoding="utf-8")
    }

    assert readers == {"client/account.py"}, (
        f"account_read_timeout is read outside the account read path: {sorted(readers)}"
    )
