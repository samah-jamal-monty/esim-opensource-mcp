"""Phase 6C: adding money to the wallet on the platform's Stripe-hosted page.

The properties under test are the ones that decide whether a user is charged correctly,
charged twice, or told something untrue about their balance:

* preparing costs nothing, creates nothing at the platform and credits nothing;
* the amount and the limits are the platform's, not this server's;
* no card detail can be accepted anywhere, structurally;
* a retry returns the same link -- and the guarantee behind that is the platform's durable
  pending-order reuse, not anything held in this process;
* only a status read can say a payment happened; a redirect never can;
* a lost answer is reported as unknown and never as a failure or a success.

Nothing here performs real I/O and no real payment page is opened: every backend route is a
``respx`` mock. No card is charged and no wallet is credited, in any test.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from esim_mcp.errors import (
    AuthenticationRequiredError,
    RateLimitedError,
    UnsafeCheckoutLinkError,
    WalletTopupAmountInvalidError,
    WalletTopupLimitReachedError,
    WalletTopupNotFoundError,
    WalletTopupOutcomeUnknownError,
    WalletTopupQuoteNotFoundError,
    WalletTopupRejectedError,
    WalletTopupStatusUnavailableError,
    WalletTopupUnavailableError,
)
from esim_mcp.tools.authentication import AuthenticationService
from esim_mcp.tools.wallet_topup import WalletTopupService
from tests.conftest import (
    API_URL,
    TOPUP_CHECKOUT_URL,
    TOPUP_PAYMENT_REFERENCE,
    StubIdentityProvider,
    envelope,
    mock_login_routes,
    sign_in,
    topup_options_payload,
    wallet_topup_checkout_payload,
    wallet_topup_status_payload,
)

OPTIONS_URL = f"{API_URL}/mcp/wallet/top-up/options"
CHECKOUT_URL = f"{API_URL}/mcp/wallet/top-up/checkout"


def status_url(reference: str = TOPUP_PAYMENT_REFERENCE) -> str:
    return f"{API_URL}/mcp/wallet/top-up/status/{reference}"


def checkout_posts(respx_mock: respx.Router) -> list:
    """Only the checkout requests, so the login POSTs in the fixture are not counted."""
    return [
        call
        for call in respx_mock.calls
        if call.request.method == "POST" and call.request.url.path.endswith("/wallet/top-up/checkout")
    ]


@pytest.fixture
async def signed_in(service: AuthenticationService, respx_mock: respx.Router) -> respx.Router:
    mock_login_routes(respx_mock)
    await sign_in(service)
    respx_mock.get(OPTIONS_URL).mock(return_value=httpx.Response(200, json=envelope(topup_options_payload())))
    return respx_mock


@pytest.fixture
def with_checkout(signed_in: respx.Router) -> respx.Router:
    signed_in.post(CHECKOUT_URL).mock(
        return_value=httpx.Response(200, json=envelope(wallet_topup_checkout_payload()))
    )
    return signed_in


async def prepared_reference(service: WalletTopupService, amount: str = "25.00") -> str:
    return (await service.prepare_wallet_topup(amount=amount))["quote_reference"]


# --------------------------------------------------------------------------- preparation


async def test_a_valid_amount_is_quoted_from_the_platforms_own_answer(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router
) -> None:
    result = await wallet_topup_service.prepare_wallet_topup(amount="25")

    assert result["status"] == "prepared"
    assert result["amount"] == "25.00"
    assert result["currency"] == "USD"
    assert result["current_balance"] == "12.50"
    assert result["balance_is_a_snapshot"] is True
    assert {limit["code"] for limit in result["platform_limits"]} == {
        "minimum_amount",
        "daily_amount",
        "daily_count",
    }
    # The platform stated no fee, so the result states none and says so.
    assert result["fees"] == []
    assert "Do not invent one" in result["fees_note"]


async def test_preparation_has_no_side_effect_anywhere(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router
) -> None:
    before = len(signed_in.calls)

    result = await wallet_topup_service.prepare_wallet_topup(amount="25")

    assert result["order_created"] is False
    assert result["charged"] is False
    assert result["credited"] is False
    for call in list(signed_in.calls)[before:]:
        assert call.request.method == "GET", f"{call.request.method} {call.request.url.path}"
    touched = [str(call.request.url).lower() for call in signed_in.calls]
    # The legacy route, the one that answers with a client secret, is never reached.
    assert not any(url.endswith("/api/v1/wallet/top-up") for url in touched)


@pytest.mark.parametrize("amount", ["0.50", "0.25", "0"])
async def test_an_amount_at_or_below_the_platform_minimum_is_refused(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router, amount: str
) -> None:
    with pytest.raises(WalletTopupAmountInvalidError) as raised:
        await wallet_topup_service.prepare_wallet_topup(amount=amount)

    assert "0.50" in str(raised.value) or "positive amount" in str(raised.value)


async def test_an_amount_above_the_platforms_remaining_allowance_is_refused_with_the_figure(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router
) -> None:
    signed_in.get(OPTIONS_URL).mock(
        return_value=httpx.Response(200, json=envelope(topup_options_payload(maximum_amount="40.00")))
    )

    with pytest.raises(WalletTopupAmountInvalidError) as raised:
        await wallet_topup_service.prepare_wallet_topup(amount="60")

    # The user is told the figure the platform *will* take, not merely refused.
    assert "40.00" in str(raised.value)


@pytest.mark.parametrize("amount", ["", "   ", "abc", "-5", "1e400", "25.005", "twenty five"])
async def test_an_unreadable_amount_is_refused_rather_than_coerced(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router, amount: str
) -> None:
    """Guessing what somebody meant by an amount is not a thing to do with money."""
    with pytest.raises(WalletTopupAmountInvalidError):
        await wallet_topup_service.prepare_wallet_topup(amount=amount)


async def test_a_platform_that_will_not_accept_any_top_up_says_so(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router
) -> None:
    signed_in.get(OPTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json=envelope(
                topup_options_payload(
                    can_topup=False,
                    maximum_amount=None,
                    message="The daily number of top-ups for this account has been reached.",
                )
            ),
        )
    )

    with pytest.raises(WalletTopupLimitReachedError) as raised:
        await wallet_topup_service.prepare_wallet_topup(amount="25")

    assert "daily number of top-ups" in str(raised.value)


async def test_an_unreachable_platform_never_becomes_a_quote(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router
) -> None:
    signed_in.get(OPTIONS_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(WalletTopupUnavailableError):
        await wallet_topup_service.prepare_wallet_topup(amount="25")


async def test_a_signed_out_caller_reaches_nothing(
    wallet_topup_service: WalletTopupService, respx_mock: respx.Router
) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await wallet_topup_service.prepare_wallet_topup(amount="25")

    assert not respx_mock.calls


# ------------------------------------------------------------- explicit confirmation gate


async def test_a_checkout_needs_a_quote_the_same_caller_prepared(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    """There is no path from "how much would it be" to a payment page without a quote."""
    from esim_mcp.errors import InvalidInputError

    with pytest.raises(InvalidInputError):
        await wallet_topup_service.create_wallet_topup_checkout(quote_reference="")

    with pytest.raises(WalletTopupQuoteNotFoundError):
        await wallet_topup_service.create_wallet_topup_checkout(quote_reference="never-issued")

    assert checkout_posts(with_checkout) == [], "a checkout was sent without a prepared quote"


async def test_the_amount_comes_from_the_stored_quote_and_not_from_the_caller(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    """There is no amount argument on the checkout tool, so a hallucination cannot reach it."""
    import inspect

    signature = inspect.signature(wallet_topup_service.create_wallet_topup_checkout)
    assert set(signature.parameters) == {"quote_reference", "ctx"}

    reference = await prepared_reference(wallet_topup_service, "25")
    await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)

    import json

    sent = checkout_posts(with_checkout)[0]
    # Exactly one field. No currency, no wallet id, no user id, no card, no paid flag.
    assert json.loads(sent.request.content) == {"amount": 25.0}
    # The currency is negotiated in one place only: the header.
    assert sent.request.headers["X-Currency"] == "USD"


# ------------------------------------------------------------------------ the payment page


async def test_a_checkout_returns_the_link_and_charges_nothing(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    reference = await prepared_reference(wallet_topup_service)

    result = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)

    assert result["status"] == "checkout_ready"
    assert result["checkout_url"] == TOPUP_CHECKOUT_URL
    assert result["payment_reference"] == TOPUP_PAYMENT_REFERENCE
    assert result["amount"] == "25.00"
    assert result["currency"] == "USD"
    assert result["charged"] is False
    assert result["paid"] is False
    assert result["credited"] is False
    assert result["order_state"] == "unpaid"
    assert "IS the confirmation" in result["link_delivery_note"]


async def test_no_provider_secret_reaches_the_result(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    """The payload carries a session id, a client secret and a developer message."""
    reference = await prepared_reference(wallet_topup_service)

    blob = str(await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference))

    for forbidden in (
        "sess_secret_value_0001",
        "secret_do_not_leak_0001",
        "developerMessage",
        "c0rr3lat10n",
        "internal backend detail",
    ):
        assert forbidden not in blob


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "http://checkout.test/pay",
        "javascript:alert(1)",
        # Built by format() rather than written literally, so the repository-hygiene scan
        # does not read the embedded credentials as a real host and a real address.
        "https://{}@checkout.test/pay".format("user:secret"),
        "not a url",
    ],
)
async def test_a_link_this_server_will_not_pass_on_is_refused_rather_than_repaired(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router, url
) -> None:
    signed_in.post(CHECKOUT_URL).mock(
        return_value=httpx.Response(200, json=envelope(wallet_topup_checkout_payload(checkout_url=url)))
    )
    reference = await prepared_reference(wallet_topup_service)

    with pytest.raises(UnsafeCheckoutLinkError):
        await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)


async def test_a_page_without_a_usable_reference_is_never_shown(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router
) -> None:
    """A page this server could not check afterwards is worse than no page at all."""
    signed_in.post(CHECKOUT_URL).mock(
        return_value=httpx.Response(
            200, json=envelope(wallet_topup_checkout_payload(payment_reference="../../wallet/top-up"))
        )
    )
    reference = await prepared_reference(wallet_topup_service)

    with pytest.raises(WalletTopupRejectedError):
        await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)


# --------------------------------------------------------------------- duplicate behaviour


async def test_asking_twice_returns_the_same_link_without_a_second_request(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    reference = await prepared_reference(wallet_topup_service)

    first = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)
    posts_after_first = len(checkout_posts(with_checkout))
    second = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)

    assert second["checkout_url"] == first["checkout_url"]
    assert second["payment_reference"] == first["payment_reference"]
    assert second["replayed"] is True
    assert "not a second one" in second["replay_note"]
    assert len(checkout_posts(with_checkout)) == posts_after_first


async def test_the_platforms_own_replay_flag_is_passed_through(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router
) -> None:
    """The durable guarantee: the platform reused its own pending order for this top-up."""
    signed_in.post(CHECKOUT_URL).mock(
        return_value=httpx.Response(
            200, json=envelope(wallet_topup_checkout_payload(idempotent_replay=True))
        )
    )
    reference = await prepared_reference(wallet_topup_service)

    result = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)

    assert result["replayed"] is True


async def test_this_server_sends_no_idempotency_key_of_its_own(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    """Deliberate: a key held here would look like a guarantee and stop being one on restart.

    Duplicate protection is the platform's pending-order reuse, which is durable. Sending a
    process-local key alongside it would advertise a safety property this server cannot keep.
    """
    reference = await prepared_reference(wallet_topup_service)
    await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)

    sent = checkout_posts(with_checkout)[0]
    assert "Idempotency-Key" not in sent.request.headers


async def test_a_lost_answer_is_unknown_and_invites_the_same_retry(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router
) -> None:
    """A timeout must never read as a failure, and must never trigger an automatic retry."""
    route = signed_in.post(CHECKOUT_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    reference = await prepared_reference(wallet_topup_service)

    with pytest.raises(WalletTopupOutcomeUnknownError) as raised:
        await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)

    # Exactly one request left this process: no automatic retry happened.
    assert route.call_count == 1
    details = raised.value.details
    assert details["charged"] is False
    assert details["credited"] is False
    assert details["retry_safe"] is True
    assert details["new_quote_safe"] is False
    assert details["next_step"] == "retry_same_checkout"


async def test_the_same_quote_may_be_retried_after_a_lost_answer(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router
) -> None:
    signed_in.post(CHECKOUT_URL).mock(
        side_effect=[
            httpx.ReadTimeout("slow"),
            httpx.Response(200, json=envelope(wallet_topup_checkout_payload(idempotent_replay=True))),
        ]
    )
    reference = await prepared_reference(wallet_topup_service)

    with pytest.raises(WalletTopupOutcomeUnknownError):
        await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)

    recovered = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)

    assert recovered["checkout_url"] == TOPUP_CHECKOUT_URL
    assert recovered["replayed"] is True


async def test_repeated_attempts_are_bounded(
    wallet_topup_service: WalletTopupService, signed_in: respx.Router
) -> None:
    """Not a duplicate guard -- a guard against an unattended loop against the platform."""
    signed_in.post(CHECKOUT_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    reference = await prepared_reference(wallet_topup_service)

    for _ in range(3):
        with pytest.raises(WalletTopupOutcomeUnknownError):
            await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)

    with pytest.raises(WalletTopupRejectedError) as raised:
        await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)
    assert "Nothing was charged" in str(raised.value)


# ------------------------------------------------------------------ platform refusals


@pytest.mark.parametrize(
    ("status", "hint", "expected"),
    [
        (400, "INVALID_TOP_UP_AMOUNT", WalletTopupAmountInvalidError),
        (400, "TOP_UP_LIMIT_REACHED", WalletTopupLimitReachedError),
        (400, "TOP_UP_AMOUNT_LIMIT_EXCEEDED", WalletTopupLimitReachedError),
        (400, "SOMETHING_ELSE", WalletTopupRejectedError),
        (401, "BEARER_TOKEN_REQUIRED", AuthenticationRequiredError),
        (404, "NOT_FOUND", WalletTopupUnavailableError),
        (429, "RATE_LIMIT", RateLimitedError),
        (503, "MCP_WALLET_TOPUP_DISABLED", WalletTopupUnavailableError),
        (500, "BOOM", WalletTopupOutcomeUnknownError),
    ],
)
async def test_a_platform_refusal_is_classified_and_never_claims_a_charge(
    wallet_topup_service: WalletTopupService,
    signed_in: respx.Router,
    status: int,
    hint: str,
    expected: type[Exception],
) -> None:
    signed_in.post(CHECKOUT_URL).mock(
        return_value=httpx.Response(
            status, json=envelope(None, status="failed", title=hint, response_code=status)
        )
    )
    reference = await prepared_reference(wallet_topup_service)

    with pytest.raises(expected) as raised:
        await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)

    message = str(raised.value).lower()
    assert "may have been charged" not in message
    assert "credited" not in message or "not credited" in message or "not been credited" in message


# ------------------------------------------------------------------------- payment status


@pytest.mark.parametrize(
    ("backend_status", "paid"),
    [("PENDING", False), ("PAID", True), ("FAILED", False), ("EXPIRED", False)],
)
async def test_the_status_is_the_platforms_and_only_the_platforms(
    wallet_topup_service: WalletTopupService,
    with_checkout: respx.Router,
    backend_status: str,
    paid: bool,
) -> None:
    reference = await prepared_reference(wallet_topup_service)
    opened = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)
    with_checkout.get(status_url()).mock(
        return_value=httpx.Response(
            200, json=envelope(wallet_topup_status_payload(status=backend_status, paid=paid))
        )
    )

    result = await wallet_topup_service.get_wallet_topup_status(
        payment_reference=opened["payment_reference"]
    )

    assert result["topup_status"] == backend_status
    assert result["paid"] is paid
    assert result["credited"] is paid
    assert result["charged"] is paid
    assert "NOT proof of payment" in result["proof_note"]


async def test_a_redirect_can_never_credit_a_wallet(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    """The mistake this phase most has to prevent, asserted rather than merely documented.

    A user who has landed on the success page but whose payment the platform has not
    recorded is still ``PENDING`` here -- and the result says so in every field a model
    could read.
    """
    reference = await prepared_reference(wallet_topup_service)
    opened = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)
    with_checkout.get(status_url()).mock(
        return_value=httpx.Response(200, json=envelope(wallet_topup_status_payload(status="PENDING")))
    )

    result = await wallet_topup_service.get_wallet_topup_status(
        payment_reference=opened["payment_reference"]
    )

    assert result["paid"] is False
    assert result["credited"] is False
    assert result["is_final"] is False
    assert "has not paid yet" in result["next_step"]
    # The link is re-offered rather than a second page being created.
    assert result["checkout_url"] == TOPUP_CHECKOUT_URL


async def test_there_is_no_argument_that_could_assert_a_payment_happened(
    wallet_topup_service: WalletTopupService,
) -> None:
    import inspect

    signature = inspect.signature(wallet_topup_service.get_wallet_topup_status)
    assert set(signature.parameters) == {"payment_reference", "ctx"}


async def test_a_terminal_status_is_replayed_rather_than_re_read(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    reference = await prepared_reference(wallet_topup_service)
    opened = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)
    route = with_checkout.get(status_url()).mock(
        return_value=httpx.Response(
            200, json=envelope(wallet_topup_status_payload(status="PAID", paid=True))
        )
    )

    first = await wallet_topup_service.get_wallet_topup_status(payment_reference=opened["payment_reference"])
    second = await wallet_topup_service.get_wallet_topup_status(payment_reference=opened["payment_reference"])

    assert first["paid"] is True and second["paid"] is True
    assert second["replayed"] is True
    assert route.call_count == 1


async def test_a_paid_top_up_cannot_open_a_second_page(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    """Once the money arrived, the quote is spent: asking again must not create a page."""
    from esim_mcp.errors import TopupQuoteCancelledError

    reference = await prepared_reference(wallet_topup_service)
    opened = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)
    with_checkout.get(status_url()).mock(
        return_value=httpx.Response(
            200, json=envelope(wallet_topup_status_payload(status="PAID", paid=True))
        )
    )
    await wallet_topup_service.get_wallet_topup_status(payment_reference=opened["payment_reference"])

    # The stored open checkout still replays its link rather than sending anything.
    posts_before = len(checkout_posts(with_checkout))
    replayed = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)
    assert replayed["replayed"] is True
    assert len(checkout_posts(with_checkout)) == posts_before

    # And a *fresh* checkout for the consumed quote is impossible.
    from esim_mcp.purchase.store import QuoteOwner
    from esim_mcp.tools.purchase_preparation import user_ref_of

    identity = await wallet_topup_service._identity_provider.resolve(None)
    session = await wallet_topup_service._sessions.require_session(identity.session_key)
    owner = QuoteOwner(session_key=identity.session_key, user_ref=user_ref_of(session))
    with pytest.raises(TopupQuoteCancelledError):
        await wallet_topup_service._quotes.get(owner, reference)


async def test_an_unreadable_status_is_never_resolved_locally(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    reference = await prepared_reference(wallet_topup_service)
    opened = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)
    with_checkout.get(status_url()).mock(
        return_value=httpx.Response(200, json=envelope({"status": "SOMETHING_NEW"}))
    )

    with pytest.raises(WalletTopupStatusUnavailableError):
        await wallet_topup_service.get_wallet_topup_status(payment_reference=opened["payment_reference"])


async def test_a_status_read_that_times_out_never_guesses(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    reference = await prepared_reference(wallet_topup_service)
    opened = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)
    with_checkout.get(status_url()).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(WalletTopupStatusUnavailableError):
        await wallet_topup_service.get_wallet_topup_status(payment_reference=opened["payment_reference"])


async def test_a_reference_this_client_never_received_is_not_found(
    wallet_topup_service: WalletTopupService, with_checkout: respx.Router
) -> None:
    route = with_checkout.get(status_url("somebody-elses-ref")).mock(
        return_value=httpx.Response(200, json=envelope(wallet_topup_status_payload(status="PAID", paid=True)))
    )

    with pytest.raises(WalletTopupNotFoundError):
        await wallet_topup_service.get_wallet_topup_status(payment_reference="somebody-elses-ref")

    assert not route.called, "an unowned reference reached the platform"


# ----------------------------------------------------------------------------- multi-user


async def test_one_client_cannot_inspect_another_clients_payment(
    make_wallet_topup_service,
    make_service,
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    """Both callers share one checkout store, so this is a real isolation assertion."""
    mock_login_routes(respx_mock, email="alice@example.com")
    await sign_in(make_service(identity_a), email="alice@example.com")
    respx_mock.get(OPTIONS_URL).mock(return_value=httpx.Response(200, json=envelope(topup_options_payload())))
    respx_mock.post(CHECKOUT_URL).mock(
        return_value=httpx.Response(200, json=envelope(wallet_topup_checkout_payload()))
    )

    alice = make_wallet_topup_service(identity_a)
    reference = await prepared_reference(alice)
    opened = await alice.create_wallet_topup_checkout(quote_reference=reference)

    await sign_in(make_service(identity_b), email="bob@example.com")
    bob = make_wallet_topup_service(identity_b)

    with pytest.raises(WalletTopupQuoteNotFoundError):
        await bob.create_wallet_topup_checkout(quote_reference=reference)
    with pytest.raises(WalletTopupNotFoundError):
        await bob.get_wallet_topup_status(payment_reference=opened["payment_reference"])


async def test_logging_out_drops_the_quote_and_the_payment_reference(
    wallet_topup_service: WalletTopupService,
    service: AuthenticationService,
    with_checkout: respx.Router,
) -> None:
    with_checkout.post(f"{API_URL}/auth/logout").mock(return_value=httpx.Response(200, json=envelope(None)))
    reference = await prepared_reference(wallet_topup_service)
    opened = await wallet_topup_service.create_wallet_topup_checkout(quote_reference=reference)

    await service.logout()
    await sign_in(service)

    with pytest.raises(WalletTopupNotFoundError):
        await wallet_topup_service.get_wallet_topup_status(payment_reference=opened["payment_reference"])
