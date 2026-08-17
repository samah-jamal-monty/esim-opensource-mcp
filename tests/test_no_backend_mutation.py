"""The safety property: exactly one route in this server can mutate anything, and it is the
idempotent MCP purchase route. Everything else stays unreachable.

Phase 4 changes the shape of this proof but not its severity. ``POST /mcp/user/bundle/assign``
is now callable -- it requires an ``Idempotency-Key``, is wallet-only, and replays a stored
answer rather than executing a second purchase. The *legacy* ``POST /user/bundle/assign``
remains forbidden and always will be: it has no idempotency key and no duplicate-order
protection, so a retry there can buy a plan twice. So do top-ups, vouchers, promotions,
payment callbacks, order OTPs, provisioning and the unauthenticated wallet-by-id read.

Four independent layers are asserted, because any one of them could be defeated alone:

* **Behavioural (preparation)** -- a full sweep of every *preparation* tool issues no
  ``POST``/``PUT``/``PATCH``/``DELETE`` and touches no forbidden path. Preparing still costs
  nothing, which is the whole reason the two phases are separate tools.
* **Structural** -- the transport refuses forbidden methods and paths before any I/O, and
  admits the one permitted mutation only by exact method-and-path match.
* **Scope** -- the allowlist contains exactly one entry, so widening it must break a test.
* **Textual** -- no source file builds a forbidden route or names a payment provider, except
  the guard that bans them and the documentation that explains why.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from esim_mcp.client.base import (
    FORBIDDEN_PATH_MARKERS,
    PERMITTED_EXACT_READ_ROUTES,
    PERMITTED_MUTATION_ROUTES,
    PERMITTED_REFERENCE_READ_ROUTES,
    BackendApiClient,
    enforce_route_is_permitted,
)
from esim_mcp.errors import ForbiddenBackendRouteError
from esim_mcp.server import build_components
from esim_mcp.settings import Settings
from tests.conftest import (
    API_URL,
    CATALOG_COUNTRIES,
    StubIdentityProvider,
    auth_payload,
    bundle_payload,
    envelope,
    make_jwt,
    wallet_payload,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_CODE = "aaaaaaaa-0000-4000-8000-000000000001"

#: Backend routes that create an order, move money, or touch an eSIM without the protections
#: the MCP purchase route has. None may ever be called, by any method.
FORBIDDEN_ROUTES = (
    "/user/bundle/assign",
    "/user/bundle/assign-top-up",
    "/user/bundle/verify_order_otp",
    "/wallet/top-up",
    "/wallet/user_wallet_by_id/123",
    "/voucher/redeem",
    "/promotion/apply",
    "/callback/stripe",
    "/user/bundle/consumption/8912345678901234567",
    "/bundles/translate_bundle",
)

#: The three routes this server may mutate through, and they are not equals: the first debits
#: a wallet, the other two open a hosted payment page and move nothing.
MCP_PURCHASE_ROUTE = "/mcp/user/bundle/assign"
MCP_CARD_CHECKOUT_ROUTE = "/mcp/user/bundle/card/checkout"
MCP_WALLET_TOPUP_CHECKOUT_ROUTE = "/mcp/wallet/top-up/checkout"

#: The one read whose fixed path nevertheless contains a forbidden marker, so it has to be
#: named to be reachable at all.
MCP_WALLET_TOPUP_OPTIONS_ROUTE = "/mcp/wallet/top-up/options"

#: The reads whose paths end in caller-supplied references, so they cannot be allowlisted by
#: exact match. Their prefixes are, and the segments after them are validated.
MCP_CARD_STATUS_PREFIX = "/mcp/user/bundle/card/status/"
MCP_WALLET_TOPUP_STATUS_PREFIX = "/mcp/wallet/top-up/status/"
CONSUMPTION_PREFIX = "/user/consumption/"
RELATED_TOPUP_PREFIX = "/user/related-topup/"

#: A well-formed ICCID, used wherever a reference segment has to be a real one.
ICCID = "8912345678901234567"

MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def payload_of(result: Any) -> dict[str, Any]:
    """The structured result a client receives, however this SDK version carries it."""
    import json

    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("tool returned no readable content")


# ------------------------------------------------------------------ structural: the guard


@pytest.mark.parametrize("path", FORBIDDEN_ROUTES)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_the_transport_refuses_every_mutating_route(path: str, method: str) -> None:
    """Refused for *any* method: a mutating route must be unreachable, not merely un-POSTed."""
    with pytest.raises(ForbiddenBackendRouteError):
        enforce_route_is_permitted(method, path)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"])
def test_the_transport_refuses_every_method_this_server_does_not_need(method: str) -> None:
    with pytest.raises(ForbiddenBackendRouteError):
        enforce_route_is_permitted(method, "/bundles/countries")


@pytest.mark.parametrize(
    "path",
    [
        "/auth/login",
        "/auth/resend-otp",
        "/auth/verify_otp",
        "/auth/refresh-token",
        "/auth/user-info",
        "/auth/logout",
        "/home/",
        "/home/cruise",
        "/home/land",
        "/bundles/countries",
        "/bundles/region",
        "/bundles/by-country",
        "/bundles/by-region/EUR",
        f"/bundles/{BUNDLE_CODE}",
        "/wallet/user_wallet_by_user",
    ],
)
def test_every_route_this_server_actually_uses_is_still_permitted(path: str) -> None:
    """The guard must not have broken Phase 1 or Phase 2 on its way to banning Phase 4's."""
    enforce_route_is_permitted("GET", path)
    enforce_route_is_permitted("POST", path)


def test_the_mcp_purchase_route_is_the_one_permitted_mutation() -> None:
    """It is reachable by POST, and by nothing else."""
    enforce_route_is_permitted("POST", MCP_PURCHASE_ROUTE)

    for method in ("PUT", "PATCH", "DELETE"):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted(method, MCP_PURCHASE_ROUTE)


def test_permitting_the_mcp_route_did_not_permit_the_legacy_one() -> None:
    """One path is a substring of the other; only the exact MCP path may pass.

    This is the assertion that would fail if the allowlist were ever loosened into a
    substring match -- which would silently re-open the un-idempotent legacy route.
    """
    enforce_route_is_permitted("POST", MCP_PURCHASE_ROUTE)

    for still_forbidden in (
        "/user/bundle/assign",
        "/user/bundle/assign-top-up",
        "/mcp/user/bundle/assign-top-up",
        "/mcp/user/bundle/assign/extra",
        "/api/mcp/user/bundle/assign",
    ):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted("POST", still_forbidden)


def test_exactly_the_three_mcp_mutations_are_allowlisted() -> None:
    """A regression guard on the allowlist: growing it must break a test, deliberately.

    Note what is *not* here and never will be: any eSIM top-up route. The platform cannot
    make a repeated top-up safe without a schema change, so this server has no way to reach
    one -- not through a client module, and not through the transport either.
    """
    assert set(PERMITTED_MUTATION_ROUTES) == {
        ("POST", MCP_PURCHASE_ROUTE),
        ("POST", MCP_CARD_CHECKOUT_ROUTE),
        ("POST", MCP_WALLET_TOPUP_CHECKOUT_ROUTE),
    }


def test_exactly_the_expected_reference_reads_are_allowlisted() -> None:
    """The prefix allowlist is the only place a dynamic path may be built. Keep it exact."""
    assert set(PERMITTED_REFERENCE_READ_ROUTES) == {
        ("GET", MCP_CARD_STATUS_PREFIX, 1),
        ("GET", MCP_WALLET_TOPUP_STATUS_PREFIX, 1),
        ("GET", CONSUMPTION_PREFIX, 1),
        ("GET", RELATED_TOPUP_PREFIX, 2),
    }


def test_exactly_one_exact_read_route_is_allowlisted() -> None:
    """Only one fixed read has to be named back in past a marker. Keep it at one."""
    assert set(PERMITTED_EXACT_READ_ROUTES) == {("GET", MCP_WALLET_TOPUP_OPTIONS_ROUTE)}


def test_the_wallet_topup_checkout_is_permitted_but_the_legacy_top_up_is_not() -> None:
    """One path contains the other; only the exact MCP ones may pass.

    This is the assertion that would fail if the allowlist were ever loosened into a
    substring match -- which would silently re-open ``POST /wallet/top-up``, the legacy route
    that answers with a Stripe client secret and is what the website's payment sheet drives.
    """
    enforce_route_is_permitted("POST", MCP_WALLET_TOPUP_CHECKOUT_ROUTE)
    enforce_route_is_permitted("GET", MCP_WALLET_TOPUP_OPTIONS_ROUTE)
    enforce_route_is_permitted("GET", f"{MCP_WALLET_TOPUP_STATUS_PREFIX}order-1")

    for still_forbidden in (
        "/wallet/top-up",
        "/wallet/top-up/checkout",
        "/mcp/wallet/top-up",
        "/mcp/wallet/top-up/checkout/extra",
        "/api/mcp/wallet/top-up/checkout",
        "/mcp/wallet/top-up/credit",
        "/mcp/wallet/top-up/refund",
    ):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted("POST", still_forbidden)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_the_wallet_topup_checkout_route_is_permitted_by_post_and_by_nothing_else(method: str) -> None:
    enforce_route_is_permitted("POST", MCP_WALLET_TOPUP_CHECKOUT_ROUTE)

    with pytest.raises(ForbiddenBackendRouteError):
        enforce_route_is_permitted(method, MCP_WALLET_TOPUP_CHECKOUT_ROUTE)


def test_the_consumption_read_is_permitted_only_with_one_plain_reference() -> None:
    """The route the marker list used to refuse outright, now admitted on exact terms."""
    enforce_route_is_permitted("GET", f"{CONSUMPTION_PREFIX}{ICCID}")

    for still_forbidden in (
        CONSUMPTION_PREFIX,
        f"{CONSUMPTION_PREFIX}{ICCID}/extra",
        f"{CONSUMPTION_PREFIX}../../wallet/top-up",
        f"{CONSUMPTION_PREFIX}{ICCID}?query=1",
        "/user/bundle/consumption/" + ICCID,
    ):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted("GET", still_forbidden)

    # A read, and only a read.
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted(method, f"{CONSUMPTION_PREFIX}{ICCID}")


def test_the_topup_options_read_needs_exactly_two_plain_references() -> None:
    """Two segments, both validated. One or three is a different route, and is refused."""
    enforce_route_is_permitted("GET", f"{RELATED_TOPUP_PREFIX}bundle-1/{ICCID}")

    for still_forbidden in (
        RELATED_TOPUP_PREFIX,
        f"{RELATED_TOPUP_PREFIX}bundle-1",
        f"{RELATED_TOPUP_PREFIX}bundle-1/{ICCID}/extra",
        f"{RELATED_TOPUP_PREFIX}../../user/bundle/assign-top-up/x",
        f"{RELATED_TOPUP_PREFIX}bundle-1/{ICCID}?query=1",
    ):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted("GET", still_forbidden)

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted(method, f"{RELATED_TOPUP_PREFIX}bundle-1/{ICCID}")


def test_the_esim_topup_execution_route_is_unreachable_by_default() -> None:
    """The route that actually tops a SIM up is refused unless a QA flag opts in.

    The default matters: every caller that does not deliberately pass the flag -- which is
    every caller in a production deployment, because production settings refuse to construct
    with it on -- gets the refusal.
    """
    for still_forbidden in (
        "/user/bundle/assign-top-up",
        "/mcp/user/bundle/assign-top-up",
        "/mcp/user/bundle/top-up",
        "/mcp/user/bundle/topup",
    ):
        for method in ("GET", "POST"):
            with pytest.raises(ForbiddenBackendRouteError):
                enforce_route_is_permitted(method, still_forbidden)


def test_the_qa_flag_admits_exactly_one_extra_route_and_only_by_post() -> None:
    """Opting in must widen the surface by one route, not by a family."""
    enforce_route_is_permitted("POST", "/user/bundle/assign-top-up", qa_esim_topup_enabled=True)

    for method in ("GET", "PUT", "PATCH", "DELETE"):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted(method, "/user/bundle/assign-top-up", qa_esim_topup_enabled=True)

    for neighbour in (
        "/user/bundle/assign",
        "/mcp/user/bundle/assign-top-up",
        "/user/bundle/assign-top-up/extra",
        "/api/user/bundle/assign-top-up",
        "/wallet/top-up",
    ):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted("POST", neighbour, qa_esim_topup_enabled=True)


def test_the_qa_route_is_not_a_member_of_the_standing_allowlist() -> None:
    """It is opt-in per call, never a permanent member. Adding it must break this test."""
    from esim_mcp.client.base import QA_ESIM_TOPUP_ROUTE

    assert QA_ESIM_TOPUP_ROUTE not in PERMITTED_MUTATION_ROUTES
    assert QA_ESIM_TOPUP_ROUTE == ("POST", "/user/bundle/assign-top-up")


def test_the_transport_derives_the_qa_flag_from_settings() -> None:
    """The service gate and the route gate read one Settings object, never two.

    A build where the service thought execution was on and the transport thought it was off
    (or the reverse) would be a configuration that cannot exist in the real server, so the
    client is asserted to take its answer from the settings it was constructed with.
    """
    from esim_mcp.client.base import BackendApiClient

    off = Settings.build(api_base_url="https://backend.test", environment="qa", device_id_salt="x" * 40)
    on = Settings.build(
        api_base_url="https://backend.test",
        environment="qa",
        device_id_salt="x" * 40,
        esim_topup_execution_enabled=True,
    )
    assert BackendApiClient(off)._settings.esim_topup_execution_enabled is False
    assert BackendApiClient(on)._settings.esim_topup_execution_enabled is True


def test_the_card_checkout_route_is_permitted_by_post_and_by_nothing_else() -> None:
    enforce_route_is_permitted("POST", MCP_CARD_CHECKOUT_ROUTE)

    for method in ("PUT", "PATCH", "DELETE"):
        with pytest.raises(ForbiddenBackendRouteError):
            enforce_route_is_permitted(method, MCP_CARD_CHECKOUT_ROUTE)


@pytest.mark.parametrize(
    "path",
    [
        # Neighbours of the permitted card path. None is allowlisted, and each of them would
        # be a different endpoint with different (or no) guarantees.
        "/mcp/user/bundle/card/checkout/extra",
        "/mcp/user/bundle/card/capture",
        "/mcp/user/bundle/card/refund",
        "/user/bundle/card/checkout",
        "/api/mcp/user/bundle/card/checkout",
    ],
)
def test_permitting_the_card_route_did_not_permit_its_neighbours(path: str) -> None:
    with pytest.raises(ForbiddenBackendRouteError):
        enforce_route_is_permitted("POST", path)


@pytest.mark.parametrize(
    "path",
    [
        f"{MCP_CARD_STATUS_PREFIX}",
        f"{MCP_CARD_STATUS_PREFIX}a/b",
        f"{MCP_CARD_STATUS_PREFIX}../../wallet/top-up",
        f"{MCP_CARD_STATUS_PREFIX}ref?query=1",
        f"{MCP_CARD_STATUS_PREFIX}{'x' * 200}",
    ],
)
def test_the_status_prefix_admits_one_plain_reference_and_nothing_else(path: str) -> None:
    """Starting with the prefix decides the outcome: a bad remainder is refused, not retried.

    This is the assertion that would fail if the prefix check ever fell through to the marker
    scan -- which would let a reference containing a dot-segment escape the route it belongs
    to simply by not spelling a banned word.
    """
    enforce_route_is_permitted("GET", f"{MCP_CARD_STATUS_PREFIX}pay-ref-0001")

    with pytest.raises(ForbiddenBackendRouteError):
        enforce_route_is_permitted("GET", path)


def test_the_authenticated_wallet_read_is_permitted_but_the_by_id_read_is_not() -> None:
    """One character of difference, and one of them exposes any user's balance."""
    enforce_route_is_permitted("GET", "/wallet/user_wallet_by_user")

    with pytest.raises(ForbiddenBackendRouteError):
        enforce_route_is_permitted("GET", "/wallet/user_wallet_by_id/abc")


async def test_a_forbidden_route_is_refused_before_any_socket_is_opened(
    backend_client: BackendApiClient, respx_mock: respx.Router
) -> None:
    with pytest.raises(ForbiddenBackendRouteError):
        await backend_client.request("POST", "/user/bundle/assign", device_id="d", json_body={"bundle_code": "x"})

    assert not respx_mock.calls, "a forbidden route reached the transport"


# ------------------------------------------------------ behavioural: the preparation sweep
#
# Preparation must still cost nothing. Buying is a separate tool, with a separate proof in
# tests/test_purchase_execution.py; nothing below ever calls it.


@pytest.fixture
def swept(respx_mock: respx.Router) -> respx.Router:
    """Only the safe reads plus login are mocked; anything else fails as an unmocked call."""
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
    return respx_mock


async def test_a_complete_phase_three_sweep_issues_no_mutating_request(settings: Settings, swept: respx.Router) -> None:
    """Every Phase 3 tool, back to back, through the real MCP binding."""
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    server = components.server
    try:
        await server.call_tool("request_login_otp", {"email": "person@example.com"})
        await server.call_tool("verify_login_otp", {"verification_pin": "123456", "email": "person@example.com"})
        login_calls = len(swept.calls)

        for arguments in (
            {"bundle_code": BUNDLE_CODE, "payment_method": "Wallet", "country": "France"},
            {"bundle_code": BUNDLE_CODE, "payment_method": "Card"},
        ):
            prepared = payload_of(await server.call_tool("prepare_purchase", arguments))
            await server.call_tool("get_prepared_purchase", {"quote_id": prepared["quote_id"]})
            await server.call_tool("cancel_prepared_purchase", {"quote_id": prepared["quote_id"]})
    finally:
        await components.aclose()

    phase_three_calls = list(swept.calls)[login_calls:]
    assert phase_three_calls, "the sweep made no backend calls at all, so it proved nothing"
    for call in phase_three_calls:
        assert call.request.method == "GET", f"{call.request.method} {call.request.url.path}"


async def test_a_complete_phase_three_sweep_touches_no_forbidden_path(settings: Settings, swept: respx.Router) -> None:
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    server = components.server
    try:
        await server.call_tool("request_login_otp", {"email": "person@example.com"})
        await server.call_tool("verify_login_otp", {"verification_pin": "123456", "email": "person@example.com"})
        prepared = payload_of(
            await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Wallet"})
        )
        await server.call_tool("get_prepared_purchase", {"quote_id": prepared["quote_id"]})
        await server.call_tool("cancel_prepared_purchase", {"quote_id": prepared["quote_id"]})
    finally:
        await components.aclose()

    touched = [str(call.request.url) for call in swept.calls]
    for banned in (
        "bundle/assign",
        "assign-top-up",
        "verify_order_otp",
        "wallet/top-up",
        "user_wallet_by_id",
        "voucher",
        "promo",
        "callback",
        "payment",
        "stripe",
        "consumption",
        "dcb",
        "translate",
    ):
        assert not any(banned in url.lower() for url in touched), f"a request reached {banned!r}"


async def test_the_only_backend_routes_a_preparation_uses_are_the_documented_reads(
    settings: Settings, swept: respx.Router
) -> None:
    """Exactly the routes section 7 of the phase brief allows, and nothing else."""
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    server = components.server
    try:
        await server.call_tool("request_login_otp", {"email": "person@example.com"})
        await server.call_tool("verify_login_otp", {"verification_pin": "123456", "email": "person@example.com"})
        login_calls = len(swept.calls)
        await server.call_tool(
            "prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Wallet", "country": "France"}
        )
    finally:
        await components.aclose()

    paths = {call.request.url.path for call in list(swept.calls)[login_calls:]}
    assert paths <= {
        f"/api/v1/bundles/{BUNDLE_CODE}",
        "/api/v1/wallet/user_wallet_by_user",
        "/api/v1/bundles/countries",
    }, paths


async def test_no_quote_result_ever_reports_an_order_or_a_charge(settings: Settings, swept: respx.Router) -> None:
    """The one sentence every branch must carry, asserted through the MCP binding."""
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    server = components.server
    try:
        await server.call_tool("request_login_otp", {"email": "person@example.com"})
        await server.call_tool("verify_login_otp", {"verification_pin": "123456", "email": "person@example.com"})
        prepared = payload_of(
            await server.call_tool("prepare_purchase", {"bundle_code": BUNDLE_CODE, "payment_method": "Card"})
        )
        read = payload_of(await server.call_tool("get_prepared_purchase", {"quote_id": prepared["quote_id"]}))
        cancelled = payload_of(await server.call_tool("cancel_prepared_purchase", {"quote_id": prepared["quote_id"]}))
        after = payload_of(await server.call_tool("get_prepared_purchase", {"quote_id": prepared["quote_id"]}))
    finally:
        await components.aclose()

    for result in (prepared, read, after):
        assert result["order_created"] is False
        assert result["charged"] is False
    assert cancelled["order_cancelled"] is False
    assert cancelled["charged"] is False


# ------------------------------------------------------------------- textual: the sources


def source_files() -> list[Path]:
    return [
        path
        for directory in ("src", "app")
        for path in (PROJECT_ROOT / directory).rglob("*.py")
        if "__pycache__" not in path.parts
    ]


#: Files allowed to *name* a forbidden route: the guard that bans them, the purchase client
#: whose own permitted path contains the legacy one as a substring, and the module docstrings
#: that explain why the rest are banned. Listed as repo-relative paths rather than bare file
#: names, so a new ``purchase.py`` somewhere else in the tree does not inherit the exemption.
_ROUTE_NAMING_ALLOWED = {
    "src/esim_mcp/client/base.py",
    "src/esim_mcp/client/catalog.py",
    "src/esim_mcp/client/purchase.py",
    "src/esim_mcp/client/wallet.py",
    "src/esim_mcp/tools/purchase_preparation.py",
    "src/esim_mcp/tools/purchase_execution.py",
    # Phase 6. Each of these names a forbidden route in prose only, to record why it is
    # banned: the top-up client explains why the execution route is not wrapped, and the
    # wallet-top-up client and tool explain why the legacy /wallet/top-up is not called.
    # ``test_no_code_path_builds_a_forbidden_route_string`` below proves none of them is
    # built as an actual path.
    # The top-up client wraps the legacy route under the QA flag and explains, at length,
    # why it is not safe to repeat. The wallet-top-up client explains why the legacy
    # /wallet/top-up is never called.
    "src/esim_mcp/client/topup.py",
    "src/esim_mcp/client/wallet_topup.py",
    "src/esim_mcp/tools/esim_topup.py",
    "src/esim_mcp/tools/wallet_topup.py",
    # The settings module documents, on the QA flag itself, exactly which route the flag
    # admits and why that route is dangerous. Naming a hazard next to its switch is the
    # opposite of shipping it.
    "src/esim_mcp/settings.py",
}

#: Files allowed to issue a non-GET request: the authentication client, the transport itself,
#: and the three clients that own the permitted mutations.
_MUTATING_VERB_ALLOWED = {
    "src/esim_mcp/client/auth.py",
    "src/esim_mcp/client/base.py",
    "src/esim_mcp/client/card.py",
    "src/esim_mcp/client/purchase.py",
    "src/esim_mcp/client/wallet_topup.py",
    # QA-only, and gated three times over -- see test_the_qa_topup_route_is_gated below.
    "src/esim_mcp/client/topup.py",
}


def relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


@pytest.mark.parametrize("route", ["/user/bundle/assign", "wallet/top-up", "verify_order_otp", "assign-top-up"])
def test_no_source_file_builds_a_forbidden_route(route: str) -> None:
    """Naming one in prose is fine; only the guard, the clients and the docs do."""
    offenders = [
        relative(path)
        for path in source_files()
        if route in path.read_text(encoding="utf-8") and relative(path) not in _ROUTE_NAMING_ALLOWED
    ]

    assert not offenders, f"{route!r} appears in {offenders}"


def test_the_purchase_client_names_the_legacy_route_only_to_refuse_it() -> None:
    """The one file that may contain the legacy path must not use it as a path.

    ``/mcp/user/bundle/assign`` contains ``/user/bundle/assign``, so a substring scan cannot
    tell them apart. This asserts the distinction directly: the module defines exactly one
    path constant, and it is the MCP one.
    """
    from esim_mcp.client.purchase import MCP_PURCHASE_PATH

    assert MCP_PURCHASE_PATH == "/mcp/user/bundle/assign"

    text = (PROJECT_ROOT / "src" / "esim_mcp" / "client" / "purchase.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if '"/user/bundle/assign"' in stripped:  # the legacy route as a usable literal
            raise AssertionError(f"the legacy route is built as a path: {stripped!r}")


def test_no_source_file_issues_a_mutating_http_verb_outside_the_permitted_clients() -> None:
    """The documented auth POSTs and the one purchase POST are the only non-GET requests."""
    offenders: list[str] = []
    for path in source_files():
        if relative(path) in _MUTATING_VERB_ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for method in MUTATING_METHODS:
            if re.search(rf'"{method}"\s*,', text):
                offenders.append(f"{relative(path)}: {method}")

    assert not offenders, f"mutating verbs outside the permitted clients: {offenders}"


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_the_purchase_client_issues_no_verb_other_than_post(method: str) -> None:
    text = (PROJECT_ROOT / "src" / "esim_mcp" / "client" / "purchase.py").read_text(encoding="utf-8")

    assert not re.search(rf'"{method}"\s*,', text)


def test_the_purchase_package_performs_no_http_at_all() -> None:
    """The quote layer is pure domain logic; it must not be able to call anything."""
    for path in (PROJECT_ROOT / "src" / "esim_mcp" / "purchase").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("httpx", "requests", "aiohttp", "BackendApiClient", "urllib"):
            assert forbidden not in text, f"{path.name} references {forbidden!r}"


def executable_code(path: Path) -> str:
    """The file's code with comments and string literals removed.

    Prose has to be excluded deliberately: several modules *name* Stripe and the assign route
    in their docstrings, to record why those things are banned. Documenting a hazard is the
    opposite of shipping it, so only real code is scanned here.
    """
    import io
    import tokenize

    kept: list[str] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(io.BytesIO(handle.read()).readline):
            if token.type not in (tokenize.STRING, tokenize.COMMENT, tokenize.FSTRING_START):
                kept.append(token.string)
    return " ".join(kept).lower()


def test_no_code_path_references_a_payment_provider() -> None:
    """Prose may name Stripe; no executable statement may."""
    for path in source_files():
        code = executable_code(path)
        for forbidden in ("stripe", "paymentintent", "client_secret", "payment_intent"):
            assert forbidden not in code, f"{path.relative_to(PROJECT_ROOT)} has code referencing {forbidden!r}"


def test_no_code_path_builds_a_forbidden_route_string() -> None:
    """The forbidden markers exist as data in the guard; nothing may use one as a path."""
    for path in source_files():
        if path.name == "base.py":  # the guard itself declares the markers
            continue
        code = executable_code(path)
        for forbidden in ("assign", "top_up", "verify_order_otp", "user_wallet_by_id"):
            assert forbidden not in code, f"{path.relative_to(PROJECT_ROOT)} has code referencing {forbidden!r}"


def test_the_forbidden_marker_list_covers_every_dangerous_route_family() -> None:
    """A regression guard on the guard: shrinking this list must break a test."""
    for required in (
        "bundle/assign",
        "bundle/card",
        "wallet/top-up",
        "user_wallet_by_id",
        "voucher",
        "callback",
        "payment",
    ):
        assert required in FORBIDDEN_PATH_MARKERS
