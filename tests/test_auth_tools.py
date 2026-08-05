"""Tool-layer behaviour: masking, challenge lifecycle, isolation and token containment."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Callable

import httpx
import pytest
import respx

from esim_mcp.errors import (
    AuthenticationRequiredError,
    InvalidInputError,
    InvalidOtpError,
    OtpStillActiveError,
)
from esim_mcp.logging_config import JsonLogFormatter, RedactionFilter
from esim_mcp.session.identity import ClientIdentityProvider
from esim_mcp.session.manager import SessionManager
from esim_mcp.session.store import InMemorySessionStore
from esim_mcp.settings import Settings
from esim_mcp.tools.authentication import AuthenticationService
from tests.conftest import (
    API_URL,
    StubIdentityProvider,
    auth_payload,
    envelope,
    make_jwt,
)

OTP = "654321"
EMAIL = "person@example.com"
PHONE = "+96171123467"
ACCESS_TOKEN = make_jwt(expires_in=3600, signature="access")
REFRESH_TOKEN = "refresh-token-secret-value"


def ok(data: object = None) -> httpx.Response:
    return httpx.Response(200, json=envelope(data))


async def authenticate(service: AuthenticationService, respx_mock: respx.Router, *, email: str = EMAIL) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=ok(auth_payload(access_token=ACCESS_TOKEN, refresh_token=REFRESH_TOKEN, email=email))
    )
    await service.request_login_otp(email=email)
    await service.verify_login_otp(verification_pin=OTP, email=email)


# ------------------------------------------------------------------ request_login_otp


async def test_request_login_otp_by_email(
    service: AuthenticationService, respx_mock: respx.Router, settings: Settings
) -> None:
    route = respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))

    result = await service.request_login_otp(email=EMAIL)

    assert result == {
        "status": "otp_requested",
        "channel": "EMAIL",
        "destination": "p***@example.com",
        "expires_in_seconds": settings.login_challenge_ttl_seconds,
    }
    body = json.loads(route.calls.last.request.content)
    assert body == {"email": EMAIL, "phone": None, "otp_channel": "EMAIL"}
    assert len(route.calls.last.request.headers["X-Device-Id"]) == 64


async def test_request_login_otp_by_phone_uses_sms(service: AuthenticationService, respx_mock: respx.Router) -> None:
    route = respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))

    result = await service.request_login_otp(phone=PHONE)

    assert result["channel"] == "SMS"
    assert result["destination"].startswith("+961")
    assert PHONE not in json.dumps(result)
    assert json.loads(route.calls.last.request.content)["phone"] == PHONE


async def test_request_login_otp_requires_an_identifier(service: AuthenticationService) -> None:
    with pytest.raises(InvalidInputError):
        await service.request_login_otp()


async def test_request_login_otp_rejects_an_unknown_channel(service: AuthenticationService) -> None:
    with pytest.raises(InvalidInputError):
        await service.request_login_otp(email=EMAIL, otp_channel="CARRIER_PIGEON")


async def test_request_login_otp_is_not_retried(service: AuthenticationService, respx_mock: respx.Router) -> None:
    route = respx_mock.post(f"{API_URL}/auth/login").mock(
        return_value=httpx.Response(
            429,
            json=envelope(None, status="failed", title="OTP_STILL_ACTIVE", response_code=429),
        )
    )

    with pytest.raises(OtpStillActiveError):
        await service.request_login_otp(email=EMAIL)

    assert route.call_count == 1


async def test_failed_login_stores_no_challenge(
    service: AuthenticationService,
    session_manager: SessionManager,
    identity_a: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(
        return_value=httpx.Response(400, json=envelope(None, status="failed", response_code=400))
    )

    with pytest.raises(InvalidInputError):
        await service.request_login_otp(email=EMAIL)

    assert await session_manager.get_challenge(identity_a.identity.session_key) is None


# ------------------------------------------------------------------- resend_login_otp


async def test_resend_requires_a_pending_challenge(service: AuthenticationService, respx_mock: respx.Router) -> None:
    route = respx_mock.post(f"{API_URL}/auth/resend-otp").mock(return_value=ok(None))

    with pytest.raises(InvalidInputError):
        await service.resend_login_otp(email=EMAIL)

    assert route.call_count == 0


async def test_resend_uses_the_same_device_id_and_channel(
    service: AuthenticationService, respx_mock: respx.Router
) -> None:
    login_route = respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    resend_route = respx_mock.post(f"{API_URL}/auth/resend-otp").mock(return_value=ok(None))

    await service.request_login_otp(email=EMAIL)
    result = await service.resend_login_otp(email=EMAIL)

    assert result["status"] == "otp_resent"
    assert result["destination"] == "p***@example.com"
    assert (
        resend_route.calls.last.request.headers["X-Device-Id"] == login_route.calls.last.request.headers["X-Device-Id"]
    )
    assert json.loads(resend_route.calls.last.request.content)["otp_channel"] == "EMAIL"


async def test_resend_rejects_a_different_identifier(service: AuthenticationService, respx_mock: respx.Router) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    resend_route = respx_mock.post(f"{API_URL}/auth/resend-otp").mock(return_value=ok(None))
    await service.request_login_otp(email=EMAIL)

    with pytest.raises(InvalidInputError):
        await service.resend_login_otp(email="someone.else@example.com")

    assert resend_route.call_count == 0


async def test_resend_is_not_retried(service: AuthenticationService, respx_mock: respx.Router) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    resend_route = respx_mock.post(f"{API_URL}/auth/resend-otp").mock(
        return_value=httpx.Response(
            429, json=envelope(None, status="failed", title="OTP_STILL_ACTIVE", response_code=429)
        )
    )
    await service.request_login_otp(email=EMAIL)

    with pytest.raises(OtpStillActiveError):
        await service.resend_login_otp(email=EMAIL)

    assert resend_route.call_count == 1


# ------------------------------------------------------------------- verify_login_otp


async def test_verify_stores_tokens_server_side_and_returns_none_of_them(
    service: AuthenticationService,
    session_manager: SessionManager,
    identity_a: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    verify_route = respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=ok(auth_payload(access_token=ACCESS_TOKEN, refresh_token=REFRESH_TOKEN))
    )

    await service.request_login_otp(email=EMAIL)
    result = await service.verify_login_otp(verification_pin=OTP, email=EMAIL)

    assert result["status"] == "authenticated"
    assert result["is_verified"] is True
    assert result["user"]["email"] == "p***@example.com"
    rendered = json.dumps(result)
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, OTP, EMAIL):
        assert secret not in rendered

    session = await session_manager.require_session(identity_a.identity.session_key)
    assert session.access_token.get_secret_value() == ACCESS_TOKEN
    assert session.refresh_token.get_secret_value() == REFRESH_TOKEN
    assert json.loads(verify_route.calls.last.request.content)["verification_pin"] == OTP


async def test_verify_clears_the_pending_challenge(
    service: AuthenticationService,
    session_manager: SessionManager,
    identity_a: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    await authenticate(service, respx_mock)

    assert await session_manager.get_challenge(identity_a.identity.session_key) is None


async def test_verify_never_persists_the_otp(
    service: AuthenticationService,
    store: InMemorySessionStore,
    respx_mock: respx.Router,
) -> None:
    await authenticate(service, respx_mock)

    dumped = json.dumps(
        {
            "sessions": [session.model_dump(mode="json") for session in store._sessions.values()],
            "challenges": [c.model_dump(mode="json") for c in store._challenges.values()],
        }
    )
    assert OTP not in dumped
    assert "verification_pin" not in dumped


@pytest.mark.parametrize("pin", ["12345", "1234567", "abcdef", "", "12 34 56"])
async def test_verify_rejects_malformed_pins(service: AuthenticationService, pin: str) -> None:
    with pytest.raises(InvalidInputError):
        await service.verify_login_otp(verification_pin=pin, email=EMAIL)


async def test_verify_is_not_retried(service: AuthenticationService, respx_mock: respx.Router) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    verify_route = respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=httpx.Response(400, json=envelope(None, status="failed", title="OTP_INVALID", response_code=400))
    )
    await service.request_login_otp(email=EMAIL)

    with pytest.raises(InvalidOtpError):
        await service.verify_login_otp(verification_pin=OTP, email=EMAIL)

    assert verify_route.call_count == 1


async def test_verify_rejects_an_identifier_that_does_not_match_the_challenge(
    service: AuthenticationService, respx_mock: respx.Router
) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    verify_route = respx_mock.post(f"{API_URL}/auth/verify_otp").mock(return_value=ok(auth_payload()))
    await service.request_login_otp(email=EMAIL)

    with pytest.raises(InvalidInputError):
        await service.verify_login_otp(verification_pin=OTP, email="someone.else@example.com")

    assert verify_route.call_count == 0


# --------------------------------------------------------------------- session tools


async def test_login_status_before_and_after_authentication(
    service: AuthenticationService, respx_mock: respx.Router
) -> None:
    unauthenticated = await service.get_login_status()
    assert unauthenticated == {"authenticated": False, "status": "unauthenticated", "pending_login": None}

    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    await service.request_login_otp(email=EMAIL)
    pending = await service.get_login_status()
    assert pending["status"] == "otp_pending"
    assert pending["pending_login"]["destination"] == "p***@example.com"

    await authenticate(service, respx_mock)
    authenticated = await service.get_login_status()
    assert authenticated["authenticated"] is True
    assert authenticated["refresh_required"] is False
    assert authenticated["session_expires_at"] is not None
    assert ACCESS_TOKEN not in json.dumps(authenticated)


async def test_login_status_does_not_call_the_backend(service: AuthenticationService, respx_mock: respx.Router) -> None:
    await authenticate(service, respx_mock)
    before = len(respx_mock.calls)

    await service.get_login_status()

    assert len(respx_mock.calls) == before


async def test_get_user_profile_returns_masked_data_and_wallet(
    service: AuthenticationService, respx_mock: respx.Router
) -> None:
    await authenticate(service, respx_mock)
    respx_mock.get(f"{API_URL}/auth/user-info").mock(
        return_value=ok(auth_payload(access_token="", refresh_token="", balance=42.0))
    )

    result = await service.get_user_profile()

    assert result["status"] == "ok"
    assert result["user"]["email"] == "p***@example.com"
    assert result["wallet"] == {"balance": 42.0, "currency": "USD"}
    assert ACCESS_TOKEN not in json.dumps(result)


async def test_get_user_profile_requires_a_session(service: AuthenticationService) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await service.get_user_profile()


async def test_get_user_profile_refreshes_an_expiring_token(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    identity_a: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    service = make_service(identity_a)
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=ok(auth_payload(access_token=make_jwt(expires_in=15), refresh_token=REFRESH_TOKEN))
    )
    await service.request_login_otp(email=EMAIL)
    await service.verify_login_otp(verification_pin=OTP, email=EMAIL)

    rotated = make_jwt(expires_in=3600, signature="rotated")
    refresh_route = respx_mock.post(f"{API_URL}/auth/refresh-token").mock(
        return_value=ok(auth_payload(access_token=rotated))
    )
    profile_route = respx_mock.get(f"{API_URL}/auth/user-info").mock(
        return_value=ok(auth_payload(access_token="", refresh_token=""))
    )

    await service.get_user_profile()

    assert refresh_route.call_count == 1
    assert profile_route.calls.last.request.headers["Authorization"] == f"Bearer {rotated}"


async def test_logout_affects_only_the_calling_client(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    session_manager: SessionManager,
    respx_mock: respx.Router,
) -> None:
    service_a = make_service(identity_a)
    service_b = make_service(identity_b)
    await authenticate(service_a, respx_mock, email="alice@example.com")
    await authenticate(service_b, respx_mock, email="bob@example.com")
    respx_mock.post(f"{API_URL}/auth/logout").mock(return_value=ok(None))

    result = await service_a.logout()

    assert result == {"status": "logged_out", "backend_confirmed": True}
    assert await session_manager.get_session(identity_a.identity.session_key) is None
    assert await session_manager.get_session(identity_b.identity.session_key) is not None
    assert (await service_b.get_login_status())["authenticated"] is True


async def test_logout_requires_a_session(service: AuthenticationService) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await service.logout()


# ------------------------------------------------------------------- multi-user rules


async def test_two_clients_get_different_device_ids_and_isolated_sessions(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    identity_a: StubIdentityProvider,
    identity_b: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    login_route = respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    service_a = make_service(identity_a)
    service_b = make_service(identity_b)

    await service_a.request_login_otp(email="alice@example.com")
    await service_b.request_login_otp(email="bob@example.com")

    device_ids = {call.request.headers["X-Device-Id"] for call in login_route.calls}
    assert len(device_ids) == 2

    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=ok(auth_payload(access_token=ACCESS_TOKEN, email="alice@example.com"))
    )
    await service_a.verify_login_otp(verification_pin=OTP, email="alice@example.com")

    assert (await service_a.get_login_status())["authenticated"] is True
    assert (await service_b.get_login_status())["authenticated"] is False


async def test_concurrent_profile_calls_refresh_once(
    make_service: Callable[[ClientIdentityProvider], AuthenticationService],
    identity_a: StubIdentityProvider,
    respx_mock: respx.Router,
) -> None:
    service = make_service(identity_a)
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=ok(auth_payload(access_token=make_jwt(expires_in=15), refresh_token=REFRESH_TOKEN))
    )
    await service.request_login_otp(email=EMAIL)
    await service.verify_login_otp(verification_pin=OTP, email=EMAIL)

    refresh_route = respx_mock.post(f"{API_URL}/auth/refresh-token").mock(
        return_value=ok(auth_payload(access_token=make_jwt(expires_in=3600, signature="rotated")))
    )
    respx_mock.get(f"{API_URL}/auth/user-info").mock(return_value=ok(auth_payload(access_token="", refresh_token="")))

    await asyncio.gather(*(service.get_user_profile() for _ in range(5)))

    assert refresh_route.call_count == 1


async def test_no_tool_result_ever_contains_a_token(service: AuthenticationService, respx_mock: respx.Router) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=ok(None))
    respx_mock.post(f"{API_URL}/auth/resend-otp").mock(return_value=ok(None))
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=ok(auth_payload(access_token=ACCESS_TOKEN, refresh_token=REFRESH_TOKEN))
    )
    respx_mock.get(f"{API_URL}/auth/user-info").mock(return_value=ok(auth_payload(access_token="", refresh_token="")))
    respx_mock.post(f"{API_URL}/auth/logout").mock(return_value=ok(None))

    results = [
        await service.request_login_otp(email=EMAIL),
        await service.resend_login_otp(email=EMAIL),
        await service.verify_login_otp(verification_pin=OTP, email=EMAIL),
        await service.get_login_status(),
        await service.get_user_profile(),
        await service.logout(),
    ]

    rendered = json.dumps(results)
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, OTP, EMAIL, "b3f1c0de-1111-2222-3333-444455556666"):
        assert secret not in rendered


async def test_tokens_and_otp_never_reach_the_logs(service: AuthenticationService, respx_mock: respx.Router) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers, root.level
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    try:
        await authenticate(service, respx_mock)
        respx_mock.get(f"{API_URL}/auth/user-info").mock(
            return_value=ok(auth_payload(access_token="", refresh_token=""))
        )
        await service.get_user_profile()
    finally:
        root.handlers, root.level = previous_handlers, previous_level

    output = stream.getvalue()
    assert output  # the flow really did log something
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, OTP, EMAIL):
        assert secret not in output
