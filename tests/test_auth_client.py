"""Backend client: headers, envelope handling, error translation and retry policy."""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import SecretStr

from esim_mcp.client.auth import AuthApiClient
from esim_mcp.client.base import BackendApiClient, classify_backend_error
from esim_mcp.errors import (
    AuthenticationRequiredError,
    BackendTimeoutError,
    BackendUnavailableError,
    ExpiredOtpError,
    InvalidBackendResponseError,
    InvalidInputError,
    InvalidOtpError,
    OtpLimitReachedError,
    OtpStillActiveError,
    OtpTooFrequentError,
    RateLimitedError,
)
from esim_mcp.models.auth import LoginRequest, OtpChannel, VerifyOtpRequest
from esim_mcp.models.common import BackendEnvelope
from tests.conftest import API_URL, auth_payload, envelope, make_jwt

DEVICE_ID = "c0ffee" * 10


async def test_login_sends_expected_headers_and_body(auth_client: AuthApiClient, respx_mock: respx.Router) -> None:
    route = respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))

    await auth_client.login(
        LoginRequest(email="person@example.com", otp_channel=OtpChannel.EMAIL),
        device_id=DEVICE_ID,
        locale="fr",
    )

    request = route.calls.last.request
    assert request.headers["X-Device-Id"] == DEVICE_ID
    assert request.headers["Accept-Language"] == "fr"
    assert "Authorization" not in request.headers
    assert "X-Refresh-Token" not in request.headers
    import json as _json

    assert _json.loads(request.content) == {
        "email": "person@example.com",
        "phone": None,
        "otp_channel": "EMAIL",
    }


async def test_phone_login_defaults_to_sms(auth_client: AuthApiClient, respx_mock: respx.Router) -> None:
    route = respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))

    await auth_client.login(LoginRequest(phone="+96171123467"), device_id=DEVICE_ID, locale="en")

    import json as _json

    body = _json.loads(route.calls.last.request.content)
    assert body["phone"] == "+96171123467"
    assert body["otp_channel"] == "SMS"


async def test_verify_otp_parses_success_envelope(auth_client: AuthApiClient, respx_mock: respx.Router) -> None:
    token = make_jwt(expires_in=900)
    route = respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=token)))
    )

    result = await auth_client.verify_otp(
        VerifyOtpRequest(user_email="person@example.com", verification_pin=SecretStr("123456")),
        device_id=DEVICE_ID,
        locale="en",
        currency="USD",
    )

    assert result.access_token.get_secret_value() == token
    assert result.user_info.email == "person@example.com"
    assert result.access_token_expiry() is not None
    assert route.calls.last.request.headers["X-Currency"] == "USD"


async def test_refresh_sends_refresh_token_header_only(auth_client: AuthApiClient, respx_mock: respx.Router) -> None:
    route = respx_mock.post(f"{API_URL}/auth/refresh-token").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload()))
    )

    await auth_client.refresh_token(
        device_id=DEVICE_ID, refresh_token=SecretStr("refresh-value"), locale="en", currency="USD"
    )

    request = route.calls.last.request
    assert request.headers["X-Refresh-Token"] == "refresh-value"
    assert "Authorization" not in request.headers


async def test_user_info_sends_bearer_token(auth_client: AuthApiClient, respx_mock: respx.Router) -> None:
    route = respx_mock.get(f"{API_URL}/auth/user-info").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token="", refresh_token="")))
    )

    result = await auth_client.get_user_info(
        device_id=DEVICE_ID, access_token=SecretStr("access-value"), locale="en", currency="EUR"
    )

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer access-value"
    assert request.headers["X-Currency"] == "EUR"
    # user-info answers with empty tokens; that must not be treated as a session.
    assert result.has_session_tokens is False
    assert result.user_info.balance == 12.5


async def test_error_envelope_on_http_200_is_still_a_failure(
    auth_client: AuthApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(
        return_value=httpx.Response(
            200,
            json=envelope(
                None,
                status="failed",
                title="OTP_REQUEST_TOO_FREQUENT",
                developer_message="internal detail that must not leak",
                response_code=429,
            ),
        )
    )

    with pytest.raises(OtpTooFrequentError) as excinfo:
        await auth_client.login(LoginRequest(email="person@example.com"), device_id=DEVICE_ID, locale="en")

    assert "internal detail" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("status_code", "title", "expected"),
    [
        (400, "The OTP provided is invalid.", InvalidOtpError),
        (404, "OTP_INVALID", InvalidOtpError),
        (400, "OTP has expired. Please request a new one.", ExpiredOtpError),
        (
            429,
            "An active OTP already exists. Please use the existing OTP or wait for it to expire",
            OtpStillActiveError,
        ),
        (400, "Maximum OTP requests per hour reached. Please try again later.", OtpLimitReachedError),
        (429, "OTP_REQUEST_TOO_FREQUENT", OtpTooFrequentError),
        (401, "401 Unauthorized", AuthenticationRequiredError),
        (403, "Forbidden", AuthenticationRequiredError),
        (429, "Too many requests", RateLimitedError),
        (400, "Request Failed", InvalidInputError),
        (503, "Exception", BackendUnavailableError),
    ],
)
async def test_backend_errors_map_to_typed_errors(
    auth_client: AuthApiClient,
    respx_mock: respx.Router,
    status_code: int,
    title: str,
    expected: type[Exception],
) -> None:
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=httpx.Response(
            status_code,
            json=envelope(None, status="failed", title=title, response_code=status_code),
        )
    )

    with pytest.raises(expected):
        await auth_client.verify_otp(
            VerifyOtpRequest(user_email="person@example.com", verification_pin=SecretStr("123456")),
            device_id=DEVICE_ID,
            locale="en",
            currency="USD",
        )


async def test_timeout_is_translated(auth_client: AuthApiClient, respx_mock: respx.Router) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(side_effect=httpx.ReadTimeout("timed out"))

    with pytest.raises(BackendTimeoutError):
        await auth_client.login(LoginRequest(email="person@example.com"), device_id=DEVICE_ID, locale="en")


async def test_connection_error_is_translated(auth_client: AuthApiClient, respx_mock: respx.Router) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(BackendUnavailableError):
        await auth_client.login(LoginRequest(email="person@example.com"), device_id=DEVICE_ID, locale="en")


async def test_non_json_response_is_translated(auth_client: AuthApiClient, respx_mock: respx.Router) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(InvalidBackendResponseError):
        await auth_client.login(LoginRequest(email="person@example.com"), device_id=DEVICE_ID, locale="en")


async def test_missing_tokens_in_verify_response_is_invalid(
    auth_client: AuthApiClient, respx_mock: respx.Router
) -> None:
    payload = auth_payload(access_token="", refresh_token="")
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(return_value=httpx.Response(200, json=envelope(payload)))

    with pytest.raises(InvalidBackendResponseError):
        await auth_client.verify_otp(
            VerifyOtpRequest(user_email="person@example.com", verification_pin=SecretStr("123456")),
            device_id=DEVICE_ID,
            locale="en",
            currency="USD",
        )


@pytest.mark.parametrize("path", ["/auth/login", "/auth/resend-otp", "/auth/verify_otp", "/auth/refresh-token"])
async def test_authentication_mutations_are_never_retried(
    auth_client: AuthApiClient, respx_mock: respx.Router, path: str
) -> None:
    route = respx_mock.post(f"{API_URL}{path}").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(BackendUnavailableError):
        if path == "/auth/login":
            await auth_client.login(LoginRequest(email="person@example.com"), device_id=DEVICE_ID, locale="en")
        elif path == "/auth/resend-otp":
            await auth_client.resend_otp(LoginRequest(email="person@example.com"), device_id=DEVICE_ID, locale="en")
        elif path == "/auth/verify_otp":
            await auth_client.verify_otp(
                VerifyOtpRequest(user_email="person@example.com", verification_pin=SecretStr("123456")),
                device_id=DEVICE_ID,
                locale="en",
                currency="USD",
            )
        else:
            await auth_client.refresh_token(
                device_id=DEVICE_ID, refresh_token=SecretStr("r"), locale="en", currency="USD"
            )

    assert route.call_count == 1


async def test_logout_mutation_is_never_retried(auth_client: AuthApiClient, respx_mock: respx.Router) -> None:
    route = respx_mock.post(f"{API_URL}/auth/logout").mock(return_value=httpx.Response(503, json=envelope(None)))

    with pytest.raises(BackendUnavailableError):
        await auth_client.logout(device_id=DEVICE_ID, access_token=SecretStr("a"), locale="en")

    assert route.call_count == 1


async def test_reads_retry_with_backoff(auth_client: AuthApiClient, respx_mock: respx.Router) -> None:
    route = respx_mock.get(f"{API_URL}/auth/user-info").mock(
        side_effect=[
            httpx.Response(503, json=envelope(None, status="failed", response_code=503)),
            httpx.Response(200, json=envelope(auth_payload(access_token="", refresh_token=""))),
        ]
    )

    result = await auth_client.get_user_info(
        device_id=DEVICE_ID, access_token=SecretStr("a"), locale="en", currency="USD"
    )

    assert route.call_count == 2
    assert result.user_info.first_name == "Test"


async def test_reads_stop_retrying_after_the_bounded_attempts(
    auth_client: AuthApiClient, respx_mock: respx.Router
) -> None:
    route = respx_mock.get(f"{API_URL}/auth/user-info").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(BackendUnavailableError):
        await auth_client.get_user_info(device_id=DEVICE_ID, access_token=SecretStr("a"), locale="en", currency="USD")

    assert route.call_count == 3


def test_envelope_success_detection() -> None:
    assert BackendEnvelope.model_validate(envelope({"a": 1})).is_success is True
    assert BackendEnvelope.model_validate(envelope(None, status="failed", response_code=400)).is_success is False
    assert BackendEnvelope.model_validate(envelope(None, response_code=500)).is_success is False
    assert BackendEnvelope.model_validate({}).is_success is False


def test_classify_without_envelope_falls_back_to_status() -> None:
    assert isinstance(classify_backend_error(401, None), AuthenticationRequiredError)
    assert isinstance(classify_backend_error(500, None), BackendUnavailableError)


def test_url_normalization(settings: object) -> None:
    client = BackendApiClient(settings)  # type: ignore[arg-type]
    assert client.url_for("/auth/login") == f"{API_URL}/auth/login"
    assert client.url_for("auth/login") == f"{API_URL}/auth/login"
