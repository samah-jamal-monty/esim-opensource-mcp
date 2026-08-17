"""Phase 6B: listing and quoting top-ups for an eSIM the user already owns.

Two properties dominate:

* **nothing here can top a SIM up.** Every tool is a read or a local quote, the execution
  route is unreachable at the transport, and no tool that performs one is registered;
* **compatibility is the platform's to state.** A plan that is not in the platform's own
  list for this SIM cannot be quoted, however similar it looks in the catalogue.

Nothing here performs real I/O: every backend route is a ``respx`` mock. No SIM is topped
up, no order is created and no money moves, in any test.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from esim_mcp.errors import (
    AuthenticationRequiredError,
    EsimNotFoundError,
    EsimTopupConfirmationRequiredError,
    EsimTopupExecutionUnavailableError,
    EsimTopupNotSupportedError,
    EsimTopupOptionsUnavailableError,
    EsimTopupOutcomeUnknownError,
    InvalidInputError,
    NoPurchasedEsimsError,
    TopupBundleIncompatibleError,
    TopupQuoteCancelledError,
    TopupQuoteExpiredError,
    TopupQuoteNotFoundError,
)
from esim_mcp.tools.authentication import AuthenticationService
from esim_mcp.tools.esim_topup import EsimTopupService
from tests.conftest import (
    API_URL,
    ICCID_A,
    ICCID_B,
    ICCID_FOREIGN,
    StubIdentityProvider,
    envelope,
    esim_payload,
    mock_login_routes,
    sign_in,
    topup_bundle_payload,
    wallet_payload,
)

PRIMARY_BUNDLE = "aaaaaaaa-0000-4000-8000-000000000001"
TOPUP_BUNDLE = "tttttttt-0000-4000-8000-000000000001"
OTHER_TOPUP_BUNDLE = "tttttttt-0000-4000-8000-000000000002"
CATALOGUE_ONLY_BUNDLE = "cccccccc-0000-4000-8000-000000000009"


def mock_esims(respx_mock: respx.Router, *payloads: dict) -> None:
    respx_mock.get(f"{API_URL}/user/my-esim").mock(
        return_value=httpx.Response(200, json=envelope(list(payloads)))
    )


def mock_options(
    respx_mock: respx.Router, *, iccid: str = ICCID_A, bundle_code: str = PRIMARY_BUNDLE, **kwargs
) -> respx.Route:
    return respx_mock.get(f"{API_URL}/user/related-topup/{bundle_code}/{iccid}").mock(**kwargs)


def mock_wallet(respx_mock: respx.Router, *, balance: float = 50.0) -> respx.Route:
    return respx_mock.get(f"{API_URL}/wallet/user_wallet_by_user").mock(
        return_value=httpx.Response(200, json=envelope(wallet_payload(balance=balance)))
    )


@pytest.fixture
async def signed_in(service: AuthenticationService, respx_mock: respx.Router) -> respx.Router:
    mock_login_routes(respx_mock)
    await sign_in(service)
    return respx_mock


@pytest.fixture
def with_one_esim(signed_in: respx.Router) -> respx.Router:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(signed_in)
    mock_options(
        signed_in,
        return_value=httpx.Response(
            200,
            json=envelope(
                [
                    topup_bundle_payload(bundle_code=TOPUP_BUNDLE, price=6.5),
                    topup_bundle_payload(
                        bundle_code=OTHER_TOPUP_BUNDLE, name="France Top-up 10GB / 30 Days", price=15.0
                    ),
                ]
            ),
        ),
    )
    return signed_in


# ------------------------------------------------------------------------ compatible options


async def test_the_options_are_the_platforms_own_compatible_list(
    esim_topup_service: EsimTopupService, with_one_esim: respx.Router
) -> None:
    result = await esim_topup_service.get_esim_topup_options()

    assert result["status"] == "ok"
    assert result["total_count"] == 2
    codes = [option["bundle_code"] for option in result["options"]]
    assert codes == [TOPUP_BUNDLE, OTHER_TOPUP_BUNDLE]
    first = result["options"][0]
    assert first["data"] == "3.0 GB"
    assert first["validity"] == "15 Day"
    assert first["price_amount"] == "6.50"
    assert first["currency"] == "USD"
    # The single sentence that stops a catalogue plan being offered as a top-up.
    assert "not from the general catalogue" in result["compatibility_note"]
    assert result["charged"] is False and result["topped_up"] is False


async def test_the_esim_is_named_only_by_a_masked_identifier(
    esim_topup_service: EsimTopupService, with_one_esim: respx.Router
) -> None:
    result = await esim_topup_service.get_esim_topup_options()

    assert result["esim"]["masked_iccid"] == "****6789"
    assert ICCID_A not in str(result)


async def test_an_empty_compatibility_list_is_a_real_answer_not_a_glitch(
    esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_options(signed_in, return_value=httpx.Response(200, json=envelope([])))

    result = await esim_topup_service.get_esim_topup_options()

    assert result["status"] == "no_options"
    assert result["options"] == []
    assert "never offer a plan from the general catalogue" in result["next_step"].lower()


async def test_an_inactive_option_is_dropped_rather_than_offered(
    esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_options(
        signed_in,
        return_value=httpx.Response(
            200,
            json=envelope(
                [
                    topup_bundle_payload(bundle_code=TOPUP_BUNDLE),
                    topup_bundle_payload(bundle_code=OTHER_TOPUP_BUNDLE, is_active=False),
                ]
            ),
        ),
    )

    result = await esim_topup_service.get_esim_topup_options()

    assert [option["bundle_code"] for option in result["options"]] == [TOPUP_BUNDLE]


async def test_a_sim_the_platform_will_not_top_up_is_refused_before_the_read(
    esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE, is_topup_allowed=False))
    route = mock_options(signed_in, return_value=httpx.Response(200, json=envelope([])))

    with pytest.raises(EsimTopupNotSupportedError):
        await esim_topup_service.get_esim_topup_options()

    assert not route.called


async def test_an_account_with_no_esims_is_told_so(
    esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in)

    with pytest.raises(NoPurchasedEsimsError):
        await esim_topup_service.get_esim_topup_options()


async def test_a_platform_outage_never_becomes_an_empty_list(
    esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    """"No top-ups available" is a claim, and it must never be made out of an error."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_options(signed_in, side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(EsimTopupOptionsUnavailableError):
        await esim_topup_service.get_esim_topup_options()


# ----------------------------------------------------------------------------- ownership


async def test_a_foreign_iccid_is_not_found_and_never_sent(
    esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    route = mock_options(signed_in, iccid=ICCID_FOREIGN, return_value=httpx.Response(200, json=envelope([])))

    with pytest.raises(EsimNotFoundError):
        await esim_topup_service.get_esim_topup_options(iccid=ICCID_FOREIGN)

    assert not route.called, "a foreign ICCID reached the platform"


async def test_a_foreign_iccid_cannot_be_quoted_either(
    esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))

    with pytest.raises(EsimNotFoundError):
        await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE, iccid=ICCID_FOREIGN)


async def test_several_esims_ask_the_user_rather_than_guessing(
    esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    mock_esims(
        signed_in,
        esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE),
        esim_payload(iccid=ICCID_B, bundle_code=PRIMARY_BUNDLE),
    )
    route = mock_options(signed_in, return_value=httpx.Response(200, json=envelope([])))

    with pytest.raises(InvalidInputError):
        await esim_topup_service.get_esim_topup_options()

    assert not route.called


async def test_a_signed_out_caller_reaches_nothing(
    esim_topup_service: EsimTopupService, respx_mock: respx.Router
) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await esim_topup_service.get_esim_topup_options()

    assert not respx_mock.calls


# ------------------------------------------------------------------------ preparation


async def test_preparing_prices_the_top_up_from_the_platforms_own_figures(
    esim_topup_service: EsimTopupService, with_one_esim: respx.Router
) -> None:
    result = await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE)

    assert result["status"] == "prepared"
    assert result["topup"]["bundle_code"] == TOPUP_BUNDLE
    assert result["topup"]["data"] == "3.0 GB"
    assert result["pricing"] == {"amount": "6.50", "currency": "USD"}
    assert result["esim"]["masked_iccid"] == "****6789"
    assert result["quote_id"]


async def test_preparation_has_no_side_effect_anywhere(
    esim_topup_service: EsimTopupService, with_one_esim: respx.Router
) -> None:
    """The whole point of a preparation phase: it costs nothing and does nothing."""
    before = len(with_one_esim.calls)

    result = await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE)

    assert result["order_created"] is False
    assert result["charged"] is False
    assert result["topped_up"] is False
    for call in list(with_one_esim.calls)[before:]:
        assert call.request.method == "GET", f"{call.request.method} {call.request.url.path}"
    touched = [str(call.request.url).lower() for call in with_one_esim.calls]
    for banned in ("assign-top-up", "bundle/assign", "wallet/top-up", "payment", "stripe"):
        assert not any(banned in url for url in touched), f"a request reached {banned!r}"


async def test_a_catalogue_plan_cannot_be_quoted_as_a_top_up(
    esim_topup_service: EsimTopupService, with_one_esim: respx.Router
) -> None:
    """The compatibility rule, enforced rather than advertised."""
    with pytest.raises(TopupBundleIncompatibleError):
        await esim_topup_service.prepare_esim_topup(bundle_code=CATALOGUE_ONLY_BUNDLE)


async def test_compatibility_and_price_are_re_read_at_preparation_time(
    esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    """A plan that stopped being compatible between listing and quoting cannot be quoted."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(signed_in)
    route = mock_options(
        signed_in,
        side_effect=[
            httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE, price=6.5)])),
            httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=OTHER_TOPUP_BUNDLE)])),
        ],
    )

    listed = await esim_topup_service.get_esim_topup_options()
    assert listed["options"][0]["bundle_code"] == TOPUP_BUNDLE

    with pytest.raises(TopupBundleIncompatibleError):
        await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE)

    assert route.call_count == 2, "the quote reused a stale compatibility list"


async def test_the_quoted_price_is_the_one_the_platform_gave_a_moment_ago(
    esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(signed_in)
    mock_options(
        signed_in,
        side_effect=[
            httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE, price=6.5)])),
            httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE, price=9.99)])),
        ],
    )

    await esim_topup_service.get_esim_topup_options()
    quoted = await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE)

    assert quoted["pricing"]["amount"] == "9.99"


async def test_every_prepared_result_says_the_top_up_cannot_be_completed_here(
    esim_topup_service: EsimTopupService, with_one_esim: respx.Router
) -> None:
    """The ceiling this phase has, stated where an assistant will actually read it."""
    result = await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE)

    assert result["can_be_completed_here"] is False
    assert "cannot carry out the top-up" in result["completion_note"]
    assert "eSIM app or on the website" in result["completion_note"]
    assert "never say it is reserved, queued, started or paid for" in result["next_step"].lower()


async def test_preparing_the_same_choice_twice_supersedes_rather_than_accumulates(
    esim_topup_service: EsimTopupService, with_one_esim: respx.Router
) -> None:
    first = await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE)
    second = await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE)

    assert first["quote_id"] != second["quote_id"]
    stale = await esim_topup_service.get_prepared_esim_topup(quote_id=first["quote_id"])
    assert stale["status"] == "cancelled"
    assert stale["charged"] is False


async def test_a_quote_for_a_different_sim_is_kept_alongside(
    esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    mock_esims(
        signed_in,
        esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE),
        esim_payload(iccid=ICCID_B, bundle_code=PRIMARY_BUNDLE),
    )
    mock_wallet(signed_in)
    for iccid in (ICCID_A, ICCID_B):
        mock_options(
            signed_in,
            iccid=iccid,
            return_value=httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)])),
        )

    first = await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE, iccid=ICCID_A)
    second = await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE, iccid=ICCID_B)

    assert (await esim_topup_service.get_prepared_esim_topup(quote_id=first["quote_id"]))["status"] == "prepared"
    assert (await esim_topup_service.get_prepared_esim_topup(quote_id=second["quote_id"]))["status"] == "prepared"


# ------------------------------------------------------------------- quote lifecycle


async def test_an_unknown_quote_reference_is_not_found(
    esim_topup_service: EsimTopupService, with_one_esim: respx.Router
) -> None:
    with pytest.raises(TopupQuoteNotFoundError):
        await esim_topup_service.get_prepared_esim_topup(quote_id="never-issued")


async def test_cancelling_is_local_and_reverses_nothing(
    esim_topup_service: EsimTopupService, with_one_esim: respx.Router
) -> None:
    prepared = await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE)
    before = len(with_one_esim.calls)

    cancelled = await esim_topup_service.cancel_prepared_esim_topup(quote_id=prepared["quote_id"])

    assert cancelled["order_cancelled"] is False
    assert cancelled["charged"] is False
    assert cancelled["topped_up"] is False
    assert len(with_one_esim.calls) == before, "cancelling contacted the platform"


async def test_an_expired_quote_is_described_rather_than_silently_reused(
    esim_topup_service: EsimTopupService,
    with_one_esim: respx.Router,
    esim_topup_quote_store,
) -> None:
    from datetime import timedelta

    from esim_mcp.topup.models import utc_now

    prepared = await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE)
    stored = esim_topup_quote_store._quotes[prepared["quote_id"]]
    esim_topup_quote_store._quotes[prepared["quote_id"]] = stored.model_copy(
        update={"expires_at": utc_now() - timedelta(seconds=1)}
    )

    read = await esim_topup_service.get_prepared_esim_topup(quote_id=prepared["quote_id"])

    assert read["status"] == "expired"
    assert read["charged"] is False


async def test_logging_out_cancels_every_top_up_quote(
    esim_topup_service: EsimTopupService,
    service: AuthenticationService,
    with_one_esim: respx.Router,
) -> None:
    """A quote naming an ICCID must not outlive the session that made it.

    Whoever signs in next on this MCP client would otherwise hold a live reference to
    somebody else's SIM.
    """
    with_one_esim.post(f"{API_URL}/auth/logout").mock(return_value=httpx.Response(200, json=envelope(None)))
    prepared = await esim_topup_service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE)

    await service.logout()
    await sign_in(service)

    # The quote is dead rather than merely invisible: reading it back reports the
    # cancellation, and acting on it raises.
    read = await esim_topup_service.get_prepared_esim_topup(quote_id=prepared["quote_id"])
    assert read["status"] == "cancelled"
    assert read["charged"] is False

    from esim_mcp.purchase.store import QuoteOwner
    from esim_mcp.tools.purchase_preparation import user_ref_of

    identity = await esim_topup_service._identity_provider.resolve(None)
    session = await esim_topup_service._sessions.require_session(identity.session_key)
    owner = QuoteOwner(session_key=identity.session_key, user_ref=user_ref_of(session))
    with pytest.raises(TopupQuoteCancelledError):
        await esim_topup_service._quotes.get(owner, prepared["quote_id"])


# ----------------------------------------------------------------------- multi-user


async def test_one_client_cannot_read_another_clients_top_up_quote(
    make_esim_topup_service,
    make_service,
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    """Both callers share one quote store, so this is a real isolation assertion."""
    mock_login_routes(respx_mock, email="alice@example.com")
    await sign_in(make_service(identity_a), email="alice@example.com")

    mock_esims(respx_mock, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(respx_mock)
    mock_options(
        respx_mock,
        return_value=httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)])),
    )

    prepared = await make_esim_topup_service(identity_a).prepare_esim_topup(bundle_code=TOPUP_BUNDLE)

    await sign_in(make_service(identity_b), email="bob@example.com")
    with pytest.raises(TopupQuoteNotFoundError):
        await make_esim_topup_service(identity_b).get_prepared_esim_topup(quote_id=prepared["quote_id"])


# ================================================================= QA top-up execution
#
# Everything below exercises `confirm_esim_topup`, which is QA-only and NOT idempotent.
# Every backend route is a `respx` mock: no SIM is topped up, no wallet is debited and no
# real request leaves this process in any test here.
#
# The properties under test are the ones that decide whether a user is charged once, twice,
# or told something untrue:
#
# * the flag gates it three times over -- registration, service, transport;
# * everything is revalidated against the platform immediately before sending;
# * the amount has to be echoed back, so a confirmation proves the quote was read;
# * exactly one request leaves, ever, for any outcome;
# * a failure object inside an HTTP 200 is not a success;
# * an unknown outcome locks the quote and never claims the wallet was untouched.

EXECUTE_URL = f"{API_URL}/user/bundle/assign-top-up"


def topup_result_payload(
    *,
    order_id: str | None = "ord-topup-0001",
    payment_status: str | None = "COMPLETED",
) -> dict:
    """The platform's own wallet-branch answer: a `PaymentIntentResponse`."""
    return {
        "order_id": order_id,
        "payment_status": payment_status,
        "publishable_key": None,
        "total_price_display": "6.50 USD",
        "has_tax": False,
    }


def execute_posts(respx_mock: respx.Router) -> list:
    """Only the execution requests, so the login POSTs are not counted."""
    return [
        call
        for call in respx_mock.calls
        if call.request.method == "POST" and call.request.url.path.endswith("/user/bundle/assign-top-up")
    ]


@pytest.fixture
def qa_ready(qa_esim_topup_service, with_one_esim: respx.Router) -> respx.Router:
    with_one_esim.post(EXECUTE_URL).mock(
        return_value=httpx.Response(200, json=envelope(topup_result_payload()))
    )
    return with_one_esim


async def prepared(service: EsimTopupService) -> dict:
    return await service.prepare_esim_topup(bundle_code=TOPUP_BUNDLE)


# ------------------------------------------------------------------------- the flag


async def test_the_execution_tool_is_not_registered_without_the_flag(settings) -> None:
    """Three gates rest on the flag. This is the first: the tool does not exist."""
    from esim_mcp.server import build_components

    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        names = {tool.name for tool in await components.server.list_tools()}
    finally:
        await components.aclose()

    assert "confirm_esim_topup" not in names


async def test_the_execution_tool_is_registered_with_the_flag(qa_topup_settings) -> None:
    from esim_mcp.server import build_components

    components = build_components(qa_topup_settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        named = {tool.name: tool for tool in await components.server.list_tools()}
    finally:
        await components.aclose()

    assert "confirm_esim_topup" in named
    annotations = named["confirm_esim_topup"].annotations
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is True
    # Stated honestly: this is the one write in this codebase that is not idempotent.
    assert annotations.idempotent_hint is False
    assert annotations.open_world_hint is True


async def test_the_service_refuses_without_the_flag(
    esim_topup_service: EsimTopupService, with_one_esim: respx.Router
) -> None:
    """The second gate: even called directly, the service refuses and sends nothing."""
    route = with_one_esim.post(EXECUTE_URL).mock(
        return_value=httpx.Response(200, json=envelope(topup_result_payload()))
    )
    quote = await prepared(esim_topup_service)

    with pytest.raises(EsimTopupExecutionUnavailableError):
        await esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    assert not route.called


def test_the_transport_refuses_the_route_without_the_flag() -> None:
    """The third gate: the path is unreachable before any I/O."""
    from esim_mcp.client.base import enforce_route_is_permitted
    from esim_mcp.errors import ForbiddenBackendRouteError

    with pytest.raises(ForbiddenBackendRouteError):
        enforce_route_is_permitted("POST", "/user/bundle/assign-top-up")

    enforce_route_is_permitted("POST", "/user/bundle/assign-top-up", qa_esim_topup_enabled=True)


@pytest.mark.parametrize(
    "neighbour",
    [
        "/user/bundle/assign",
        "/mcp/user/bundle/assign-top-up",
        "/user/bundle/assign-top-up/extra",
        "/api/user/bundle/assign-top-up",
    ],
)
def test_the_qa_flag_admits_that_one_route_and_no_neighbour(neighbour: str) -> None:
    from esim_mcp.client.base import enforce_route_is_permitted
    from esim_mcp.errors import ForbiddenBackendRouteError

    with pytest.raises(ForbiddenBackendRouteError):
        enforce_route_is_permitted("POST", neighbour, qa_esim_topup_enabled=True)


def test_production_settings_refuse_to_construct_with_the_flag_on() -> None:
    """The flag cannot reach production by configuration drift: the process will not boot."""
    from esim_mcp.settings import Settings

    with pytest.raises(ValueError, match="must be false in production"):
        Settings.build(
            api_base_url="https://backend.test",
            environment="production",
            device_id_salt="x" * 40,
            esim_topup_execution_enabled=True,
        )


# ------------------------------------------------------------- successful confirmation


async def test_a_confirmed_top_up_reports_the_platforms_own_result(
    qa_esim_topup_service: EsimTopupService, qa_ready: respx.Router
) -> None:
    quote = await prepared(qa_esim_topup_service)
    assert quote["can_be_completed_here"] is True
    assert quote["requires_explicit_confirmation"] is True
    assert quote["idempotent"] is False

    result = await qa_esim_topup_service.confirm_esim_topup(
        quote_id=quote["quote_id"], confirmed_amount=quote["confirm_amount"]
    )

    assert result["status"] == "topped_up"
    assert result["order_created"] is True
    assert result["charged"] is True
    assert result["topped_up"] is True
    assert result["order_id"] == "ord-topup-0001"
    assert result["pricing"] == {"amount": "6.50", "currency": "USD"}
    assert result["esim"]["masked_iccid"] == "****6789"
    assert ICCID_A not in str(result)


async def test_the_request_is_the_platforms_own_contract(
    qa_esim_topup_service: EsimTopupService, qa_ready: respx.Router
) -> None:
    """Exactly `AssignTopUpRequest`, with the exact Wallet spelling the backend compares."""
    import json

    quote = await prepared(qa_esim_topup_service)
    await qa_esim_topup_service.confirm_esim_topup(
        quote_id=quote["quote_id"], confirmed_amount=quote["confirm_amount"]
    )

    sent = execute_posts(qa_ready)[0]
    assert json.loads(sent.request.content) == {
        "iccid": ICCID_A,
        "bundle_code": TOPUP_BUNDLE,
        "payment_type": "Wallet",
    }
    # No amount, no price, no user id, no order id: the platform derives all of them.
    assert sent.request.headers["X-Currency"] == "USD"


async def test_the_prepared_quote_carries_everything_the_user_must_hear(
    qa_esim_topup_service: EsimTopupService, qa_ready: respx.Router
) -> None:
    """Requirement 4, asserted field by field."""
    quote = await prepared(qa_esim_topup_service)

    assert quote["esim"]["masked_iccid"] == "****6789"
    assert quote["topup"]["name"]
    assert quote["topup"]["data"] == "3.0 GB"
    assert quote["topup"]["validity"] == "15 Day"
    assert quote["pricing"] == {"amount": "6.50", "currency": "USD"}
    assert quote["payment_method"] == "Wallet"
    assert quote["wallet"]["balance"] == "50.00"
    assert quote["wallet"]["sufficient"] is True
    assert "debits the user's wallet IMMEDIATELY" in quote["debit_warning"]
    assert "CANNOT BE REPEATED SAFELY" in quote["debit_warning"]
    assert "WAIT for their answer" in quote["next_step"]


# ------------------------------------------------------ explicit confirmation required


@pytest.mark.parametrize("confirmed", ["", "   ", "6.51", "7", "abc", "650"])
async def test_a_confirmation_that_does_not_match_the_quote_is_refused(
    qa_esim_topup_service: EsimTopupService, qa_ready: respx.Router, confirmed: str
) -> None:
    """The echo-back gate: a confirmation can only come from something that read the quote."""
    quote = await prepared(qa_esim_topup_service)

    with pytest.raises(EsimTopupConfirmationRequiredError):
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount=confirmed)

    assert execute_posts(qa_ready) == [], "a mismatched confirmation reached the platform"


async def test_a_refused_confirmation_leaves_the_quote_usable(
    qa_esim_topup_service: EsimTopupService, qa_ready: respx.Router
) -> None:
    """Refusing must not burn the quote -- the user can still agree to the right amount."""
    quote = await prepared(qa_esim_topup_service)

    with pytest.raises(EsimTopupConfirmationRequiredError):
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="9.99")

    result = await qa_esim_topup_service.confirm_esim_topup(
        quote_id=quote["quote_id"], confirmed_amount="6.50"
    )
    assert result["topped_up"] is True


# ------------------------------------------------------------------------ revalidation


async def test_a_foreign_iccid_is_rejected_before_any_execution(
    make_esim_topup_service, qa_topup_settings, service, respx_mock: respx.Router
) -> None:
    """Ownership is re-established at confirm time, and a foreign SIM never reaches the platform."""
    mock_login_routes(respx_mock)
    await sign_in(service)
    mock_esims(respx_mock, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(respx_mock)
    mock_options(
        respx_mock,
        return_value=httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)])),
    )
    route = respx_mock.post(EXECUTE_URL).mock(
        return_value=httpx.Response(200, json=envelope(topup_result_payload()))
    )
    qa = make_esim_topup_service(StubIdentityProvider("client-a"), qa_topup_settings)
    quote = await prepared(qa)

    # The SIM leaves the account between preparing and confirming.
    mock_esims(respx_mock, esim_payload(iccid=ICCID_B, bundle_code=PRIMARY_BUNDLE))

    with pytest.raises(EsimNotFoundError):
        await qa.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    assert not route.called, "a foreign ICCID reached the platform"


async def test_a_plan_that_stopped_being_compatible_is_rejected(
    qa_esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(signed_in)
    mock_options(
        signed_in,
        side_effect=[
            httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE, price=6.5)])),
            httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=OTHER_TOPUP_BUNDLE)])),
        ],
    )
    route = signed_in.post(EXECUTE_URL).mock(
        return_value=httpx.Response(200, json=envelope(topup_result_payload()))
    )
    quote = await prepared(qa_esim_topup_service)

    with pytest.raises(TopupBundleIncompatibleError):
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    assert not route.called


async def test_a_price_that_moved_since_the_quote_is_rejected(
    qa_esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    """Never charge an amount the user did not hear."""
    from esim_mcp.errors import EsimTopupRejectedError

    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(signed_in)
    mock_options(
        signed_in,
        side_effect=[
            httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE, price=6.5)])),
            httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE, price=9.99)])),
        ],
    )
    route = signed_in.post(EXECUTE_URL).mock(
        return_value=httpx.Response(200, json=envelope(topup_result_payload()))
    )
    quote = await prepared(qa_esim_topup_service)

    with pytest.raises(EsimTopupRejectedError) as raised:
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    assert "9.99" in str(raised.value)
    assert not route.called


async def test_an_insufficient_wallet_is_rejected_before_execution(
    qa_esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    from esim_mcp.errors import InsufficientWalletBalanceError

    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_options(
        signed_in,
        return_value=httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)])),
    )
    signed_in.get(f"{API_URL}/wallet/user_wallet_by_user").mock(
        side_effect=[
            httpx.Response(200, json=envelope(wallet_payload(balance=50.0))),
            httpx.Response(200, json=envelope(wallet_payload(balance=1.0))),
        ]
    )
    route = signed_in.post(EXECUTE_URL).mock(
        return_value=httpx.Response(200, json=envelope(topup_result_payload()))
    )
    quote = await prepared(qa_esim_topup_service)

    with pytest.raises(InsufficientWalletBalanceError) as raised:
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    assert "5.50" in str(raised.value)
    assert not route.called


async def test_an_expired_quote_is_rejected(
    qa_esim_topup_service: EsimTopupService, qa_ready: respx.Router, esim_topup_quote_store
) -> None:
    from datetime import timedelta

    from esim_mcp.topup.models import utc_now

    quote = await prepared(qa_esim_topup_service)
    stored = esim_topup_quote_store._quotes[quote["quote_id"]]
    esim_topup_quote_store._quotes[quote["quote_id"]] = stored.model_copy(
        update={"expires_at": utc_now() - timedelta(seconds=1)}
    )

    with pytest.raises(TopupQuoteExpiredError):
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    assert execute_posts(qa_ready) == []


async def test_an_insufficient_balance_at_quote_time_makes_the_quote_unconfirmable(
    qa_esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(signed_in, balance=1.0)
    mock_options(
        signed_in,
        return_value=httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)])),
    )

    quote = await prepared(qa_esim_topup_service)

    assert quote["can_be_completed_here"] is False
    assert quote["wallet"]["sufficient"] is False
    assert quote["wallet"]["shortfall"] == "5.50"
    assert "shortfall" in quote["precondition"]


# ------------------------------------------------------- failure inside an HTTP success


@pytest.mark.parametrize(
    "payload",
    [
        {"order_id": None, "payment_status": "COMPLETED"},
        {"order_id": "ord-topup-0001", "payment_status": "PENDING"},
        {"order_id": "ord-topup-0001", "payment_status": None},
    ],
)
async def test_a_success_envelope_that_is_not_a_completion_is_never_reported_as_success(
    qa_esim_topup_service: EsimTopupService, signed_in: respx.Router, payload: dict
) -> None:
    """Requirement 9: a 200 is not a confirmation. Only an explicit completion is."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(signed_in)
    mock_options(
        signed_in,
        return_value=httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)])),
    )
    signed_in.post(EXECUTE_URL).mock(
        return_value=httpx.Response(200, json=envelope(topup_result_payload(**payload)))
    )
    quote = await prepared(qa_esim_topup_service)

    with pytest.raises(EsimTopupOutcomeUnknownError):
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")


async def test_a_failed_envelope_inside_an_http_200_is_a_failure_not_a_success(
    qa_esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    from esim_mcp.errors import InsufficientWalletBalanceError

    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(signed_in)
    mock_options(
        signed_in,
        return_value=httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)])),
    )
    signed_in.post(EXECUTE_URL).mock(
        return_value=httpx.Response(
            200,
            json=envelope(
                None, status="failed", title="INSUFFICIENT_WALLET_BALANCE", response_code=400
            ),
        )
    )
    quote = await prepared(qa_esim_topup_service)

    with pytest.raises(InsufficientWalletBalanceError):
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")


# ---------------------------------------------------- unknown outcomes and the one lock


@pytest.mark.parametrize(
    "failure",
    [
        {"side_effect": httpx.ReadTimeout("slow")},
        {"side_effect": httpx.ConnectError("dropped")},
        {"return_value": httpx.Response(500, json={"boom": True})},
        {"return_value": httpx.Response(200, content=b"not json at all")},
    ],
)
async def test_a_lost_or_unreadable_answer_is_unknown_and_never_a_failure(
    qa_esim_topup_service: EsimTopupService, signed_in: respx.Router, failure: dict
) -> None:
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(signed_in)
    mock_options(
        signed_in,
        return_value=httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)])),
    )
    route = signed_in.post(EXECUTE_URL).mock(**failure)
    quote = await prepared(qa_esim_topup_service)

    with pytest.raises(EsimTopupOutcomeUnknownError) as raised:
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    # Exactly one request left this process: nothing retried automatically.
    assert route.call_count == 1
    details = raised.value.details
    # Never claims the wallet was untouched.
    assert details["charged"] is None
    assert details["topped_up"] is None
    assert details["retry_safe"] is False
    assert details["new_topup_safe"] is False
    assert details["next_step"] == "check_account_state"
    assert set(details["check_with"]) >= {"get_my_esims", "get_esim_consumption", "get_user_profile"}
    message = str(raised.value).lower()
    assert "may or may not" in message
    assert "do not try again" in message


async def test_an_unknown_outcome_locks_the_quote_against_reuse(
    qa_esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    """Requirement 11: no automatic reuse after an unknown outcome, ever."""
    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(signed_in)
    mock_options(
        signed_in,
        return_value=httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)])),
    )
    route = signed_in.post(EXECUTE_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    quote = await prepared(qa_esim_topup_service)

    with pytest.raises(EsimTopupOutcomeUnknownError):
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    for _ in range(3):
        with pytest.raises(EsimTopupOutcomeUnknownError):
            await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    assert route.call_count == 1, "a locked quote was sent to the platform again"


async def test_confirming_the_same_quote_twice_replays_rather_than_charging_twice(
    qa_esim_topup_service: EsimTopupService, qa_ready: respx.Router
) -> None:
    quote = await prepared(qa_esim_topup_service)

    first = await qa_esim_topup_service.confirm_esim_topup(
        quote_id=quote["quote_id"], confirmed_amount="6.50"
    )
    second = await qa_esim_topup_service.confirm_esim_topup(
        quote_id=quote["quote_id"], confirmed_amount="6.50"
    )

    assert first["order_id"] == second["order_id"]
    assert second["replayed"] is True
    assert "never tell them they were charged twice" in second["replay_note"].lower()
    assert len(execute_posts(qa_ready)) == 1, "a repeated confirmation reached the platform twice"


async def test_a_definitively_refused_top_up_cannot_be_confirmed_again_either(
    qa_esim_topup_service: EsimTopupService, signed_in: respx.Router
) -> None:
    """One quote, one attempt -- even when the platform said nothing was charged."""
    from esim_mcp.errors import EsimTopupRejectedError

    mock_esims(signed_in, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(signed_in)
    mock_options(
        signed_in,
        return_value=httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)])),
    )
    route = signed_in.post(EXECUTE_URL).mock(
        return_value=httpx.Response(
            400, json=envelope(None, status="failed", title="TOPUP_FAILED", response_code=400)
        )
    )
    quote = await prepared(qa_esim_topup_service)

    with pytest.raises(EsimTopupRejectedError):
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")
    with pytest.raises(EsimTopupRejectedError):
        await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    assert route.call_count == 1


async def test_a_successful_top_up_consumes_the_quote(
    qa_esim_topup_service: EsimTopupService, qa_ready: respx.Router
) -> None:
    """Requirement 10: the quote is spent, so it can never become a second top-up."""
    from esim_mcp.purchase.store import QuoteOwner
    from esim_mcp.tools.purchase_preparation import user_ref_of

    quote = await prepared(qa_esim_topup_service)
    await qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    identity = await qa_esim_topup_service._identity_provider.resolve(None)
    session = await qa_esim_topup_service._sessions.require_session(identity.session_key)
    owner = QuoteOwner(session_key=identity.session_key, user_ref=user_ref_of(session))
    with pytest.raises(TopupQuoteCancelledError):
        await qa_esim_topup_service._quotes.get(owner, quote["quote_id"])


async def test_two_concurrent_confirmations_of_one_quote_send_one_request(
    qa_esim_topup_service: EsimTopupService, qa_ready: respx.Router
) -> None:
    """The per-quote lock, under a real race."""
    import asyncio

    quote = await prepared(qa_esim_topup_service)

    results = await asyncio.gather(
        qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50"),
        qa_esim_topup_service.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50"),
        return_exceptions=True,
    )

    assert len(execute_posts(qa_ready)) == 1, "a race reached the platform twice"
    succeeded = [r for r in results if isinstance(r, dict) and r.get("topped_up")]
    assert len(succeeded) == 2, "one of the two callers got neither a result nor a replay"


# ----------------------------------------------------------------------- multi-user


async def test_one_user_cannot_confirm_another_users_top_up_quote(
    make_esim_topup_service,
    make_service,
    qa_topup_settings,
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    """Both callers share one quote store and one execution store, so this is real isolation."""
    mock_login_routes(respx_mock, email="alice@example.com")
    await sign_in(make_service(identity_a), email="alice@example.com")
    mock_esims(respx_mock, esim_payload(iccid=ICCID_A, bundle_code=PRIMARY_BUNDLE))
    mock_wallet(respx_mock)
    mock_options(
        respx_mock,
        return_value=httpx.Response(200, json=envelope([topup_bundle_payload(bundle_code=TOPUP_BUNDLE)])),
    )
    route = respx_mock.post(EXECUTE_URL).mock(
        return_value=httpx.Response(200, json=envelope(topup_result_payload()))
    )

    alice = make_esim_topup_service(identity_a, qa_topup_settings)
    quote = await prepared(alice)

    await sign_in(make_service(identity_b), email="bob@example.com")
    bob = make_esim_topup_service(identity_b, qa_topup_settings)

    with pytest.raises(TopupQuoteNotFoundError):
        await bob.confirm_esim_topup(quote_id=quote["quote_id"], confirmed_amount="6.50")

    assert not route.called, "one user's confirmation reached the platform for another's quote"
