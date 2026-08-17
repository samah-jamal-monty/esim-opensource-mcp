"""Picking an eSIM by number: "show consumption for eSIM number 2".

An ICCID is a credential-shaped identifier this server never reads out, so a user who owns
several eSIMs needs some other way to say which one they mean. ``get_my_esims`` numbers what
it shows, and the follow-up tools take that number.

The number is the whole risk. It is short, guessable, and one lookup away from a full ICCID,
so the properties asserted here are the ones that would matter if they broke:

* a number resolves **only** against the listing this client-and-user was itself shown, and
  never against another client's, another account's, or an older one;
* resolving a number costs **no** extra ``GET /user/my-esim``. Re-reading would be slow and,
  worse, would re-number: the platform returns eSIMs in its own order, so "number 2" could
  come to mean a different SIM between the listing and the follow-up;
* a number outside the listing is a validation error, and a listing that is gone is a
  different answer from that -- one is fixed by picking again, the other by listing again;
* the guidance tells the model to show the number and the last four digits, and nothing more
  of the identifier.

Nothing here buys, tops up, provisions or installs anything: every backend route is a
``respx`` mock and only reads are wired.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from esim_mcp.errors import (
    AuthenticationRequiredError,
    ConsumptionUnavailableError,
    EsimSelectionOutOfRangeError,
    EsimSelectionUnavailableError,
    InvalidInputError,
)
from esim_mcp.session.identity import ClientIdentityProvider
from esim_mcp.tools.account import AccountService
from esim_mcp.tools.authentication import AuthenticationService
from esim_mcp.tools.consumption import ConsumptionService
from esim_mcp.tools.esim_topup import EsimTopupService
from tests.conftest import (
    API_URL,
    ICCID_A,
    ICCID_B,
    ICCID_FOREIGN,
    StubIdentityProvider,
    auth_payload,
    bundle_payload,
    consumption_payload,
    envelope,
    esim_payload,
    make_jwt,
)

MY_ESIM_URL = f"{API_URL}/user/my-esim"
BUNDLE_A = "aaaaaaaa-0000-4000-8000-000000000001"

#: Two accounts, so "the same client signed in as somebody else" is testable rather than
#: assumed. The platform's own user token is what the owner reference is derived from.
USER_ONE = "b3f1c0de-1111-2222-3333-444455556666"
USER_TWO = "c4a2d1ef-9999-8888-7777-666655554444"


def login_payload(*, email: str, user_token: str, access_token: str | None = None) -> dict[str, Any]:
    """A backend auth payload for a named account.

    ``auth_payload`` fixes the user token, and this file's whole point is telling two accounts
    apart, so the token is overridden here rather than shared.
    """
    payload = auth_payload(access_token=access_token or make_jwt(subject=user_token), email=email)
    payload["user_token"] = user_token
    return payload


def mock_esims(router: respx.Router, *payloads: dict[str, Any]) -> respx.Route:
    return router.get(MY_ESIM_URL).mock(return_value=httpx.Response(200, json=envelope(list(payloads))))


def mock_consumption(router: respx.Router, iccid: str, **kwargs: Any) -> respx.Route:
    return router.get(f"{API_URL}/user/consumption/{iccid}", name=f"consumption-{iccid}").mock(**kwargs)


def mock_options(router: respx.Router, bundle_code: str, iccid: str, **kwargs: Any) -> respx.Route:
    return router.get(f"{API_URL}/user/related-topup/{bundle_code}/{iccid}").mock(**kwargs)


def esim_calls(router: respx.Router) -> list[httpx.Request]:
    return [call.request for call in router.calls if call.request.url.path.endswith("/user/my-esim")]


async def sign_in_as(
    router: respx.Router,
    auth_service: AuthenticationService,
    *,
    email: str,
    user_token: str,
) -> None:
    """Drive a real OTP login for one named account."""
    router.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))
    router.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=httpx.Response(200, json=envelope(login_payload(email=email, user_token=user_token)))
    )
    await auth_service.request_login_otp(email=email)
    await auth_service.verify_login_otp(verification_pin="123456", email=email)


@pytest.fixture
async def signed_in(service: AuthenticationService, respx_mock: respx.Router) -> respx.Router:
    respx_mock.post(f"{API_URL}/auth/logout").mock(return_value=httpx.Response(200, json=envelope(None)))
    await sign_in_as(respx_mock, service, email="a@example.com", user_token=USER_ONE)
    return respx_mock


@pytest.fixture
async def listed(account_service: AccountService, signed_in: respx.Router) -> respx.Router:
    """One signed-in user who has just been shown a two-eSIM numbered list."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A), esim_payload(iccid=ICCID_B, name="Asia 10GB / 30 Days"))
    await account_service.get_my_esims()
    return signed_in


# ----------------------------------------------------------------- the numbered listing


async def test_every_listed_esim_carries_a_number_and_only_a_four_digit_tail(
    account_service: AccountService, signed_in: respx.Router
) -> None:
    """Sequential from 1, and the identifier is present in full but shortened for display."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A), esim_payload(iccid=ICCID_B))

    result = await account_service.get_my_esims()

    assert [esim["number"] for esim in result["esims"]] == [1, 2]
    assert [esim["iccid_last4"] for esim in result["esims"]] == [ICCID_A[-4:], ICCID_B[-4:]]
    for esim in result["esims"]:
        assert len(esim["iccid_last4"]) == 4
        # The full identifier stays in the structured result: the follow-up tools need it,
        # and requirement two is about what is *displayed*, not about what is carried.
        assert esim["iccid"] in {ICCID_A, ICCID_B}


async def test_a_listed_esim_carries_everything_the_numbered_display_needs(
    account_service: AccountService, signed_in: respx.Router
) -> None:
    """Number, plan, data, validity, status, purchase date, tail. Nothing has to be inferred."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))

    esim = (await account_service.get_my_esims())["esims"][0]

    assert esim["number"] == 1
    assert esim["plan"]["name"] == "France 5GB / 30 Days"
    assert esim["plan"]["data"] == "5.0 GB"
    assert esim["plan"]["validity"] == "30 Day"
    assert esim["order_status"] == "success"
    assert esim["purchased_at"] == "2025-08-06T09:29:14+00:00"
    assert esim["iccid_last4"] == ICCID_A[-4:]


async def test_the_guidance_says_to_show_the_number_and_only_the_last_four_digits(
    account_service: AccountService, signed_in: respx.Router
) -> None:
    """The result carries the full ICCID, so the instruction not to show it has to be explicit."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))

    result = await account_service.get_my_esims()

    guidance = result["next_step"].lower()
    assert "numbered list" in guidance
    assert "iccid_last4" in guidance
    assert "****1234" in guidance
    assert "never appear in your reply unless the user explicitly asks" in guidance

    selection = result["selection_note"].lower()
    assert "esim_number=2" in selection
    assert "do not call get_my_esims again first" in selection
    assert "never carry a number over from an earlier list" in selection


async def test_the_tool_descriptions_teach_the_numbered_selection(settings: Any) -> None:
    """A model that is never told about the numbers will keep asking for identifiers."""
    from esim_mcp.server import build_components

    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        described = {tool.name: tool.description.lower() for tool in await components.server.list_tools()}
    finally:
        await components.aclose()

    listing = described["get_my_esims"]
    assert "numbered list" in listing
    assert "iccid_last4" in listing
    assert "never put it in a reply unless the user explicitly asks" in listing
    assert "esim_number=2" in listing

    for name in ("get_esim_consumption", "get_esim_topup_options"):
        assert "esim_number=2" in described[name], name
        assert "esim_selection_unavailable" in described[name], name
        assert "esim_selection_out_of_range" in described[name], name


# ------------------------------------------------------------------- same-session success


async def test_a_number_resolves_to_that_esim_in_the_same_session(
    consumption_service: ConsumptionService, listed: respx.Router
) -> None:
    """"Number 2" reaches the platform as the second listed eSIM's own identifier."""
    route = mock_consumption(
        listed, ICCID_B, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    result = await consumption_service.get_esim_consumption(esim_number=2)

    assert result["status"] == "ok"
    assert route.call_count == 1, "the second eSIM's own usage route was not the one called"
    assert result["esim"]["masked_iccid"] == f"****{ICCID_B[-4:]}"


async def test_number_one_and_number_two_are_not_the_same_esim(
    consumption_service: ConsumptionService, listed: respx.Router
) -> None:
    """The obvious off-by-one, asserted rather than assumed."""
    first = mock_consumption(
        listed, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )
    second = mock_consumption(
        listed, ICCID_B, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    await consumption_service.get_esim_consumption(esim_number=1)
    await consumption_service.get_esim_consumption(esim_number=2)

    assert first.call_count == 1
    assert second.call_count == 1


async def test_selecting_by_number_makes_no_further_my_esim_call(
    consumption_service: ConsumptionService, listed: respx.Router
) -> None:
    """The point of remembering the listing: the slowest read this server makes is skipped."""
    mock_consumption(listed, ICCID_B, return_value=httpx.Response(200, json=envelope(consumption_payload())))
    before = len(esim_calls(listed))

    await consumption_service.get_esim_consumption(esim_number=2)

    assert before == 1, "the listing itself should have read the eSIMs exactly once"
    assert len(esim_calls(listed)) == before, "resolving a number re-read the eSIM list"


async def test_the_identifier_path_still_reads_the_list(
    consumption_service: ConsumptionService, listed: respx.Router
) -> None:
    """The older argument is unchanged: it still resolves against a fresh, owned list."""
    mock_consumption(listed, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload())))
    before = len(esim_calls(listed))

    await consumption_service.get_esim_consumption(iccid=ICCID_A)

    assert len(esim_calls(listed)) == before + 1


async def test_top_up_options_resolve_the_same_number_without_re_reading_the_list(
    esim_topup_service: EsimTopupService, listed: respx.Router
) -> None:
    """The second tool that takes a number, and it skips the list read for the same reason."""
    route = mock_options(
        listed,
        BUNDLE_A,
        ICCID_B,
        return_value=httpx.Response(200, json=envelope([bundle_payload(bundle_code="t-0001")])),
    )
    before = len(esim_calls(listed))

    result = await esim_topup_service.get_esim_topup_options(esim_number=2)

    assert result["status"] == "ok"
    assert route.call_count == 1
    assert len(esim_calls(listed)) == before, "resolving a number re-read the eSIM list"


# ------------------------------------------------------------------- rejected selections


@pytest.mark.parametrize("number", [3, 4, 99])
async def test_a_number_beyond_the_listing_is_a_validation_error(
    consumption_service: ConsumptionService, listed: respx.Router, number: int
) -> None:
    """Two eSIMs were listed, so three is not a slow platform -- it is a bad argument."""
    before = len(listed.calls)

    with pytest.raises(EsimSelectionOutOfRangeError) as excinfo:
        await consumption_service.get_esim_consumption(esim_number=number)

    assert excinfo.value.code == "esim_selection_out_of_range"
    assert "2 eSIM(s)" in excinfo.value.message
    assert isinstance(excinfo.value, InvalidInputError), "existing invalid-input handling must still catch this"
    assert len(listed.calls) == before, "an out-of-range number reached the backend"


@pytest.mark.parametrize("number", [0, -1, -99])
async def test_a_number_below_one_is_refused_before_any_lookup(
    consumption_service: ConsumptionService, listed: respx.Router, number: int
) -> None:
    before = len(listed.calls)

    with pytest.raises(EsimSelectionOutOfRangeError) as excinfo:
        await consumption_service.get_esim_consumption(esim_number=number)

    assert "start at 1" in excinfo.value.message
    assert len(listed.calls) == before


async def test_passing_both_a_number_and_an_identifier_is_refused(
    consumption_service: ConsumptionService, listed: respx.Router
) -> None:
    """They could name different eSIMs, and only the caller knows which it meant."""
    before = len(listed.calls)

    with pytest.raises(InvalidInputError) as excinfo:
        await consumption_service.get_esim_consumption(esim_number=1, iccid=ICCID_B)

    assert "not both" in excinfo.value.message
    assert len(listed.calls) == before


async def test_an_empty_listing_leaves_no_number_to_select(
    account_service: AccountService, consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    """An account that owns nothing is a different fact from a listing that is gone."""
    mock_esims(signed_in)
    await account_service.get_my_esims()

    with pytest.raises(EsimSelectionOutOfRangeError) as excinfo:
        await consumption_service.get_esim_consumption(esim_number=1)

    assert "no eSIM to select" in excinfo.value.message
    assert "never invent one" in excinfo.value.message


# ------------------------------------------------------- missing, stale and invalidated


async def test_a_number_with_no_listing_at_all_asks_for_the_list_again(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    """Signed in, but nothing has been listed yet. No backend call is worth making."""
    before = len(signed_in.calls)

    with pytest.raises(EsimSelectionUnavailableError) as excinfo:
        await consumption_service.get_esim_consumption(esim_number=1)

    assert excinfo.value.code == "esim_selection_unavailable"
    message = excinfo.value.message.lower()
    assert "call get_my_esims" in message
    assert "never guess" in message
    assert len(signed_in.calls) == before, "an unresolvable number reached the backend"


async def test_signing_out_invalidates_the_listing(
    service: AuthenticationService,
    consumption_service: ConsumptionService,
    listed: respx.Router,
) -> None:
    """A listing outliving its session would hand the next user a map to somebody's SIM."""
    await service.logout()

    # No session at all now, so the session check fires before the listing is even consulted.
    with pytest.raises(AuthenticationRequiredError):
        await consumption_service.get_esim_consumption(esim_number=1)

    # And after signing back in, the listing really is gone rather than merely unreachable.
    await sign_in_as(listed, service, email="a@example.com", user_token=USER_ONE)

    with pytest.raises(EsimSelectionUnavailableError):
        await consumption_service.get_esim_consumption(esim_number=1)


async def test_signing_in_as_another_account_invalidates_the_listing(
    service: AuthenticationService,
    consumption_service: ConsumptionService,
    listed: respx.Router,
) -> None:
    """Same MCP client, different eSIM user. "Number 2" now refers to somebody else's SIM.

    This is the case a session-key-only scope would miss entirely: the client identity has not
    changed, so only the authenticated user reference can tell the two listings apart.
    """
    await sign_in_as(listed, service, email="b@example.com", user_token=USER_TWO)

    with pytest.raises(EsimSelectionUnavailableError):
        await consumption_service.get_esim_consumption(esim_number=1)

    assert not [call for call in listed.calls if "/user/consumption/" in call.request.url.path]


async def test_a_reconnected_client_cannot_resolve_the_previous_sessions_numbers(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    make_consumption_service: Callable[[ClientIdentityProvider], ConsumptionService],
    listed: respx.Router,
) -> None:
    """A reconnect is a new transport principal, so it is a new session and a new listing.

    Modelled the way the Streamable HTTP transport actually behaves: the reconnected client
    resolves to a different session key, signs in as the *same* account, and still has to list
    the eSIMs again -- because the numbers it would be resolving were shown to a session that
    no longer exists.
    """
    reconnected = StubIdentityProvider("client-a-after-reconnect")
    await sign_in_as(listed, make_service(reconnected), email="a@example.com", user_token=USER_ONE)

    with pytest.raises(EsimSelectionUnavailableError):
        await make_consumption_service(reconnected).get_esim_consumption(esim_number=1)


async def test_a_new_listing_replaces_the_previous_one(
    account_service: AccountService, consumption_service: ConsumptionService, listed: respx.Router
) -> None:
    """Numbers mean the latest list, never an older one, and a shorter list really is shorter."""
    mock_esims(listed, esim_payload(iccid=ICCID_A))
    await account_service.get_my_esims()

    with pytest.raises(EsimSelectionOutOfRangeError):
        await consumption_service.get_esim_consumption(esim_number=2)


# ------------------------------------------------------------------ multi-user isolation


async def test_two_users_cannot_resolve_each_others_numbers(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    make_account_service: Callable[[ClientIdentityProvider], AccountService],
    make_consumption_service: Callable[[ClientIdentityProvider], ConsumptionService],
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    """Two clients, two accounts, one number each -- and each number means its own SIM.

    Both callers share one selection store and one session store, which is what makes this
    worth asserting: isolation here is a property of the owner scoping, not of the fixtures
    happening to keep them apart.
    """
    await sign_in_as(respx_mock, make_service(identity_a), email="a@example.com", user_token=USER_ONE)
    mock_esims(respx_mock, esim_payload(iccid=ICCID_A))
    await make_account_service(identity_a).get_my_esims()

    await sign_in_as(respx_mock, make_service(identity_b), email="b@example.com", user_token=USER_TWO)
    mock_esims(respx_mock, esim_payload(iccid=ICCID_FOREIGN))
    await make_account_service(identity_b).get_my_esims()

    mine = mock_consumption(
        respx_mock, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )
    theirs = mock_consumption(
        respx_mock, ICCID_FOREIGN, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    await make_consumption_service(identity_a).get_esim_consumption(esim_number=1)
    await make_consumption_service(identity_b).get_esim_consumption(esim_number=1)

    assert mine.call_count == 1, "A's number 1 did not resolve to A's own eSIM"
    assert theirs.call_count == 1, "B's number 1 did not resolve to B's own eSIM"


async def test_a_second_users_listing_does_not_disturb_the_first(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    make_account_service: Callable[[ClientIdentityProvider], AccountService],
    make_consumption_service: Callable[[ClientIdentityProvider], ConsumptionService],
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    """B listing after A must not renumber, replace or invalidate A's listing."""
    await sign_in_as(respx_mock, make_service(identity_a), email="a@example.com", user_token=USER_ONE)
    mock_esims(respx_mock, esim_payload(iccid=ICCID_A), esim_payload(iccid=ICCID_B))
    await make_account_service(identity_a).get_my_esims()

    await sign_in_as(respx_mock, make_service(identity_b), email="b@example.com", user_token=USER_TWO)
    mock_esims(respx_mock, esim_payload(iccid=ICCID_FOREIGN))
    await make_account_service(identity_b).get_my_esims()

    route = mock_consumption(
        respx_mock, ICCID_B, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    await make_consumption_service(identity_a).get_esim_consumption(esim_number=2)

    assert route.call_count == 1, "A's number 2 stopped meaning A's second eSIM"


async def test_one_users_logout_leaves_the_others_listing_alone(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    make_account_service: Callable[[ClientIdentityProvider], AccountService],
    make_consumption_service: Callable[[ClientIdentityProvider], ConsumptionService],
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    respx_mock.post(f"{API_URL}/auth/logout").mock(return_value=httpx.Response(200, json=envelope(None)))
    await sign_in_as(respx_mock, make_service(identity_a), email="a@example.com", user_token=USER_ONE)
    mock_esims(respx_mock, esim_payload(iccid=ICCID_A))
    await make_account_service(identity_a).get_my_esims()

    await sign_in_as(respx_mock, make_service(identity_b), email="b@example.com", user_token=USER_TWO)
    mock_esims(respx_mock, esim_payload(iccid=ICCID_FOREIGN))
    await make_account_service(identity_b).get_my_esims()

    await make_service(identity_a).logout()

    route = mock_consumption(
        respx_mock, ICCID_FOREIGN, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )
    await make_consumption_service(identity_b).get_esim_consumption(esim_number=1)

    assert route.call_count == 1, "one client's logout dropped another client's listing"


# --------------------------------------------------------------- a failure is not a zero


async def test_a_failed_reading_for_a_selected_esim_is_never_zero_usage(
    consumption_service: ConsumptionService, listed: respx.Router
) -> None:
    """The selection path must not become a shortcut around the honesty rules."""
    mock_consumption(listed, ICCID_B, return_value=httpx.Response(500, json=envelope(None, status="failed")))

    with pytest.raises(ConsumptionUnavailableError) as excinfo:
        await consumption_service.get_esim_consumption(esim_number=2)

    message = excinfo.value.message.lower()
    assert "never guess how much data is left" in message
    for invented in ("0 gb", "zero", "full allowance"):
        assert invented not in message


async def test_a_selected_esim_with_no_readings_is_reported_as_nothing_yet(
    consumption_service: ConsumptionService, listed: respx.Router
) -> None:
    """An empty platform answer stays "nothing reported", not "nothing used"."""
    mock_consumption(listed, ICCID_B, return_value=httpx.Response(200, json=envelope(None)))

    result = await consumption_service.get_esim_consumption(esim_number=2)

    assert result["status"] == "no_usage_reported"
    assert result["usage_reported"] is False
    assert "usage" not in result
    assert "do not say they have used nothing" in result["next_step"].lower()


# ------------------------------------------------------------ identical in every environment


def test_the_selection_flow_has_no_environment_conditions() -> None:
    """Requirement eight, asserted against the source rather than promised in a comment.

    A QA-only or production-only branch here would mean the numbering behaved one way where
    it was tested and another way where it mattered.
    """
    package = Path(__file__).resolve().parents[1] / "src" / "esim_mcp" / "selection"
    for path in package.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("is_production", "Environment.", "esim_topup_execution_enabled", "getenv"):
            assert forbidden not in source, f"{path.name} branches on the environment ({forbidden})"


def test_the_stored_listing_holds_no_install_credential() -> None:
    """A listing outlives a single result, so it must not carry what installs a SIM."""
    from esim_mcp.selection.models import SelectedEsim

    fields = set(SelectedEsim.model_fields)
    for credential in ("activation_code", "qr_code_value", "smdp_address"):
        assert credential not in fields, f"a selection record stores {credential}"


async def test_a_selection_record_carries_no_token_and_no_raw_owner(
    account_service: AccountService,
    signed_in: respx.Router,
    esim_selection_store: Any,
    identity_a: StubIdentityProvider,
) -> None:
    """Both halves of the owner are digests, and no secret rides along with them."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    await account_service.get_my_esims()

    stored = esim_selection_store._selections[identity_a.identity.session_key]
    blob = stored.model_dump_json().lower()

    assert USER_ONE not in blob, "the backend user id was stored raw"
    assert "a@example.com" not in blob
    for secret in ("access_token", "refresh_token", "bearer"):
        assert secret not in blob
    # The full ICCID is stored on purpose -- it is the thing a number resolves to.
    assert ICCID_A in stored.entries[0].iccid
