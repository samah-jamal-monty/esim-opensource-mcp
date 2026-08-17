"""Phase 6A: live eSIM usage.

The properties under test are the ones that would mislead a traveller if they broke:

* the figures are the platform's, copied, never derived;
* an empty answer is reported as "nothing yet", never as zero usage;
* an ICCID that is not the caller's is *not found*, and never reaches the platform;
* the ICCID is only ever returned masked, and never appears in a log record.

Nothing here performs real I/O: every backend route is a ``respx`` mock.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from esim_mcp.errors import (
    ConsumptionUnavailableError,
    EsimNotFoundError,
    InvalidInputError,
    NoConsumptionDataError,
    NoPurchasedEsimsError,
    RateLimitedError,
)
from esim_mcp.logging_config import RedactionFilter
from esim_mcp.tools.authentication import AuthenticationService
from esim_mcp.tools.consumption import ConsumptionService
from tests.conftest import (
    API_URL,
    ICCID_A,
    ICCID_B,
    ICCID_FOREIGN,
    StubIdentityProvider,
    consumption_payload,
    envelope,
    esim_payload,
    mock_login_routes,
    sign_in,
)


def mock_esims(respx_mock: respx.Router, *payloads: dict) -> None:
    respx_mock.get(f"{API_URL}/user/my-esim").mock(
        return_value=httpx.Response(200, json=envelope(list(payloads)))
    )


def mock_consumption(respx_mock: respx.Router, iccid: str, **kwargs) -> respx.Route:
    return respx_mock.get(f"{API_URL}/user/consumption/{iccid}").mock(**kwargs)


@pytest.fixture
async def signed_in(
    service: AuthenticationService, respx_mock: respx.Router
) -> respx.Router:
    mock_login_routes(respx_mock)
    await sign_in(service)
    return respx_mock


# --------------------------------------------------------------------- authenticated success


async def test_a_signed_in_user_gets_the_platforms_own_figures(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    mock_consumption(
        signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    result = await consumption_service.get_esim_consumption()

    assert result["status"] == "ok"
    assert result["usage_reported"] is True
    usage = result["usage"]
    assert usage["total_data"] == "5.0 GB"
    assert usage["used_data"] == "1.25 GB"
    assert usage["remaining_data"] == "3.75 GB"
    assert usage["plan_status"] == "Active"
    assert usage["expiry_date"] == "2026-09-10T12:00:00+00:00"
    # Derived from the two live figures and from nothing else.
    assert usage["usage_percent"] == 25.0


async def test_the_iccid_only_ever_leaves_this_server_masked(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    mock_consumption(
        signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    result = await consumption_service.get_esim_consumption()

    assert result["esim"]["masked_iccid"] == "****6789"
    assert ICCID_A not in str(result)


async def test_nothing_secret_or_provider_shaped_reaches_the_result(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    """The payload carries a subscriber id and a developer message. Neither may survive."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    mock_consumption(
        signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    blob = str(await consumption_service.get_esim_consumption())

    for forbidden in ("sub_secret_0001", "developerMessage", "internal backend detail"):
        assert forbidden not in blob


async def test_the_iccid_never_survives_into_a_log_record(
    consumption_service: ConsumptionService, signed_in: respx.Router, caplog: pytest.LogCaptureFixture
) -> None:
    """The ICCID *is* in the request path, so this is a real leak the filter has to catch.

    Unlike every other route this server calls, the consumption path carries a credential-
    shaped identifier as a path segment -- and the transport logs ``http_path``. The
    redaction filter is what stops that reaching a handler, so it is asserted here rather
    than assumed.
    """
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    mock_consumption(
        signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    with caplog.at_level(logging.DEBUG):
        caplog.handler.addFilter(RedactionFilter())
        await consumption_service.get_esim_consumption()

    rendered = caplog.text + " ".join(str(record.__dict__) for record in caplog.records)
    assert ICCID_A not in rendered
    assert "consumption" in rendered, "the sweep captured nothing, so it proved nothing"


async def test_an_unlimited_plan_reports_no_percentage_rather_than_a_made_up_one(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    mock_consumption(
        signed_in,
        ICCID_A,
        return_value=httpx.Response(
            200,
            json=envelope(consumption_payload(data_allocated=0, data_used=12.0, data_remaining=0)),
        ),
    )

    result = await consumption_service.get_esim_consumption()

    assert "usage_percent" not in result["usage"]
    assert "percent_note" in result


# ------------------------------------------------------------------------------ no eSIMs


@pytest.mark.parametrize("payload", [None, []])
async def test_an_account_with_no_esims_is_told_so_and_the_platform_is_never_asked(
    consumption_service: ConsumptionService, signed_in: respx.Router, payload
) -> None:
    signed_in.get(f"{API_URL}/user/my-esim").mock(return_value=httpx.Response(200, json=envelope(payload)))
    route = mock_consumption(signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(None)))

    with pytest.raises(NoPurchasedEsimsError):
        await consumption_service.get_esim_consumption()

    assert not route.called


# -------------------------------------------------------------------- one and many eSIMs


async def test_one_esim_needs_no_identifier(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    route = mock_consumption(
        signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    await consumption_service.get_esim_consumption()

    assert route.called


async def test_several_esims_ask_the_user_rather_than_guessing(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A), esim_payload(iccid=ICCID_B))
    route = mock_consumption(
        signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    with pytest.raises(InvalidInputError) as raised:
        await consumption_service.get_esim_consumption()

    assert "more than one eSIM" in str(raised.value)
    assert not route.called, "a guess reached the platform"


async def test_the_named_esim_of_several_is_the_one_read(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A), esim_payload(iccid=ICCID_B, label="Spain trip"))
    wrong = mock_consumption(
        signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )
    right = mock_consumption(
        signed_in,
        ICCID_B,
        return_value=httpx.Response(200, json=envelope(consumption_payload(data_used=4.0, data_remaining=1.0))),
    )

    result = await consumption_service.get_esim_consumption(iccid=ICCID_B)

    assert right.called and not wrong.called
    assert result["esim"]["label"] == "Spain trip"
    assert result["usage"]["used_data"] == "4.0 GB"


# ------------------------------------------------------------------------ ownership


async def test_an_iccid_the_account_does_not_own_is_not_found_and_never_sent(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    """The ownership boundary. It must hold *before* the platform is asked anything."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    route = mock_consumption(
        signed_in, ICCID_FOREIGN, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    with pytest.raises(EsimNotFoundError):
        await consumption_service.get_esim_consumption(iccid=ICCID_FOREIGN)

    assert not route.called, "a foreign ICCID reached the platform"


async def test_an_unknown_iccid_and_a_foreign_one_are_indistinguishable(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    """Nothing here may be used to learn that an eSIM exists on somebody else's account."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))

    with pytest.raises(EsimNotFoundError) as foreign:
        await consumption_service.get_esim_consumption(iccid=ICCID_FOREIGN)
    with pytest.raises(EsimNotFoundError) as invented:
        await consumption_service.get_esim_consumption(iccid="8931080019000000000")

    assert str(foreign.value) == str(invented.value)


@pytest.mark.parametrize(
    ("bad", "expected"),
    [
        # Blank with several eSIMs: the tool asks which one rather than guessing.
        ("", InvalidInputError),
        ("   ", InvalidInputError),
        # Anything else simply is not in the caller's list, and "not yours" and "not a real
        # identifier" deliberately give the same answer.
        ("not-an-iccid", EsimNotFoundError),
        ("../../wallet/top-up", EsimNotFoundError),
        ("8931080019123456789/extra", EsimNotFoundError),
        ("12345", EsimNotFoundError),
        ("' OR 1=1 --", EsimNotFoundError),
    ],
)
async def test_a_malformed_identifier_is_refused_before_any_request(
    consumption_service: ConsumptionService, signed_in: respx.Router, bad: str, expected: type[Exception]
) -> None:
    """Nothing unvalidated ever becomes part of a URL: the list is consulted first."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A), esim_payload(iccid=ICCID_B))
    route = mock_consumption(
        signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    with pytest.raises(expected):
        await consumption_service.get_esim_consumption(iccid=bad)

    assert not route.called


def test_the_client_refuses_a_malformed_iccid_before_building_a_path() -> None:
    """Defence in depth: even reached directly, the client validates the segment itself."""
    from esim_mcp.client.consumption import require_iccid

    assert require_iccid(f"  {ICCID_A} ") == ICCID_A
    for bad in ("", "abc", "../../wallet/top-up", f"{ICCID_A}/extra", f"{ICCID_A}?q=1", "1234"):
        with pytest.raises(InvalidInputError):
            require_iccid(bad)


# ------------------------------------------------------ empty / unavailable provider data


@pytest.mark.parametrize("payload", [None, {}, "not-an-object"])
async def test_an_empty_answer_is_reported_as_nothing_yet_and_never_as_zero(
    consumption_service: ConsumptionService, signed_in: respx.Router, payload
) -> None:
    """The single most dangerous thing this tool could do is turn silence into a number."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    mock_consumption(signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(payload)))

    result = await consumption_service.get_esim_consumption()

    assert result["status"] == "no_usage_reported"
    assert result["usage_reported"] is False
    # No usage keys at all: a model must not be able to read a zero out of this.
    assert "usage" not in result
    assert "0" not in result["message"]
    assert "used nothing" in result["next_step"] or "Do NOT say they have used nothing" in result["next_step"]


async def test_an_expired_bundle_with_no_provider_reading_is_still_not_zero(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, plan_started=False, bundle_expired=True))
    mock_consumption(signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(None)))

    result = await consumption_service.get_esim_consumption()

    assert result["usage_reported"] is False
    assert result["esim"]["bundle_expired"] is True


async def test_an_expired_bundle_with_a_reading_reports_the_reading(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    """An expired plan can still have final figures, and they are the platform's to state."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_expired=True))
    mock_consumption(
        signed_in,
        ICCID_A,
        return_value=httpx.Response(
            200,
            json=envelope(
                consumption_payload(data_used=5.0, data_remaining=0.0, plan_status="Expired")
            ),
        ),
    )

    result = await consumption_service.get_esim_consumption()

    assert result["usage"]["plan_status"] == "Expired"
    assert result["usage"]["remaining_data"] == "0.0 GB"
    assert result["usage"]["usage_percent"] == 100.0


# ----------------------------------------------------------------------- typed backend errors


async def test_a_provider_timeout_is_reported_as_unreadable_never_as_a_figure(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    mock_consumption(signed_in, ICCID_A, side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(ConsumptionUnavailableError):
        await consumption_service.get_esim_consumption()


async def test_a_backend_outage_is_reported_as_unreadable(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    mock_consumption(signed_in, ICCID_A, return_value=httpx.Response(503, json=envelope(None, status="failed")))

    with pytest.raises(ConsumptionUnavailableError):
        await consumption_service.get_esim_consumption()


async def test_a_profile_the_platform_cannot_resolve_reports_no_data_not_an_error(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    """The eSIM is on the account but has no provisioned profile behind it yet."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    mock_consumption(
        signed_in,
        ICCID_A,
        return_value=httpx.Response(
            400,
            json=envelope(None, status="failed", title="USER_PROFILE_NOT_FOUND", response_code=400),
        ),
    )

    with pytest.raises(NoConsumptionDataError):
        await consumption_service.get_esim_consumption()


async def test_rate_limiting_is_surfaced_as_itself(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    mock_consumption(
        signed_in, ICCID_A, return_value=httpx.Response(429, json=envelope(None, status="failed", response_code=429))
    )

    with pytest.raises(RateLimitedError):
        await consumption_service.get_esim_consumption()


# ------------------------------------------------------------------------ nothing mutates


async def test_reading_usage_issues_only_gets(
    consumption_service: ConsumptionService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A))
    mock_consumption(
        signed_in, ICCID_A, return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )
    before = len(signed_in.calls)

    await consumption_service.get_esim_consumption()

    for call in list(signed_in.calls)[before:]:
        assert call.request.method == "GET", f"{call.request.method} {call.request.url.path}"


async def test_a_signed_out_caller_cannot_read_usage(
    consumption_service: ConsumptionService, respx_mock: respx.Router
) -> None:
    from esim_mcp.errors import AuthenticationRequiredError

    with pytest.raises(AuthenticationRequiredError):
        await consumption_service.get_esim_consumption()

    assert not respx_mock.calls, "an unauthenticated read reached the platform"


# ----------------------------------------------------------------------------- multi-user


async def test_one_client_cannot_read_another_clients_usage(
    make_consumption_service,
    make_service,
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    """Two MCP clients, two sessions, two eSIM lists. Neither can name the other's SIM.

    ``identity_b`` never signs in, so its session does not exist -- which is the state a
    second connected user is in before they log in, and the state that must never be served
    from somebody else's session.
    """
    mock_login_routes(respx_mock)
    await sign_in(make_service(identity_a))

    respx_mock.get(f"{API_URL}/user/my-esim").mock(
        return_value=httpx.Response(200, json=envelope([esim_payload(iccid=ICCID_A)]))
    )
    respx_mock.get(f"{API_URL}/user/consumption/{ICCID_A}").mock(
        return_value=httpx.Response(200, json=envelope(consumption_payload()))
    )

    from esim_mcp.errors import AuthenticationRequiredError

    assert (await make_consumption_service(identity_a).get_esim_consumption())["usage_reported"] is True

    with pytest.raises(AuthenticationRequiredError):
        await make_consumption_service(identity_b).get_esim_consumption(iccid=ICCID_A)
