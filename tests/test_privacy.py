"""Identifier privacy: complete emails, phone numbers and ids never leave the server.

The Phase 1 QA run showed the assistant repeating a user's full email address after login
and again inside the profile answer. Two layers are asserted here:

* **Tool results** -- no code path may return a complete identifier, whatever the backend
  answers with, and whatever the caller passed in;
* **Logs** -- a full identifier that reaches a log record is masked before a handler sees it.

The instruction layer (telling the model not to retype an address the *user* typed) is
covered in ``test_tool_guidance.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
import respx

from esim_mcp.logging_config import JsonLogFormatter, RedactionFilter
from esim_mcp.tools.authentication import AuthenticationService
from esim_mcp.tools.catalog import CatalogService
from tests.conftest import (
    API_URL,
    CATALOG_COUNTRIES,
    auth_payload,
    bundle_payload,
    envelope,
    home_payload,
    make_jwt,
)

FULL_EMAIL = "mohammad.tokko@example.com"
FULL_PHONE = "+96171123467"
MASKED_EMAIL = "m***@example.com"
MASKED_PHONE = "+961******67"
USER_TOKEN = "b3f1c0de-1111-2222-3333-444455556666"


def rendered(value: Any) -> str:
    return json.dumps(value, default=str)


@pytest.fixture
def signed_in(respx_mock: respx.Router) -> respx.Router:
    """Wire a full login whose backend payload carries the complete identifiers."""
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=httpx.Response(
            200,
            json=envelope(auth_payload(access_token=make_jwt(), email=FULL_EMAIL, msisdn=FULL_PHONE)),
        )
    )
    respx_mock.get(f"{API_URL}/auth/user-info").mock(
        return_value=httpx.Response(
            200,
            json=envelope(auth_payload(access_token="", refresh_token="", email=FULL_EMAIL, msisdn=FULL_PHONE)),
        )
    )
    return respx_mock


# ------------------------------------------------------------------------ tool results


async def test_login_confirmation_returns_only_masked_identifiers(
    service: AuthenticationService, signed_in: respx.Router
) -> None:
    await service.request_login_otp(email=FULL_EMAIL)

    result = await service.verify_login_otp(verification_pin="123456", email=FULL_EMAIL)

    blob = rendered(result)
    assert FULL_EMAIL not in blob
    assert FULL_PHONE not in blob
    assert result["user"]["email"] == MASKED_EMAIL
    assert result["user"]["phone"] == MASKED_PHONE


async def test_the_otp_destination_is_masked(service: AuthenticationService, signed_in: respx.Router) -> None:
    result = await service.request_login_otp(email=FULL_EMAIL)

    assert result["destination"] == MASKED_EMAIL
    assert FULL_EMAIL not in rendered(result)


async def test_profile_never_returns_the_complete_email_or_phone(
    service: AuthenticationService, signed_in: respx.Router
) -> None:
    """The user-info payload holds both in full; neither may survive into the result."""
    await service.request_login_otp(email=FULL_EMAIL)
    await service.verify_login_otp(verification_pin="123456", email=FULL_EMAIL)

    result = await service.get_user_profile()

    blob = rendered(result)
    assert FULL_EMAIL not in blob
    assert FULL_PHONE not in blob
    assert result["user"]["email"] == MASKED_EMAIL
    assert result["user"]["phone"] == MASKED_PHONE


async def test_profile_keeps_the_useful_non_sensitive_fields(
    service: AuthenticationService, signed_in: respx.Router
) -> None:
    await service.request_login_otp(email=FULL_EMAIL)
    await service.verify_login_otp(verification_pin="123456", email=FULL_EMAIL)

    result = await service.get_user_profile()

    assert result["user"]["language"] == "en"
    assert result["user"]["is_verified"] is True
    assert result["user"]["first_name"] == "Test"
    assert result["wallet"] == {"balance": 12.5, "currency": "USD"}


async def test_profile_masks_even_when_the_session_never_recorded_a_mask(
    service: AuthenticationService, respx_mock: respx.Router
) -> None:
    """Masking happens where the value is used, so no fallback path can leak it."""
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=make_jwt(), email=None, msisdn=None)))
    )
    respx_mock.get(f"{API_URL}/auth/user-info").mock(
        return_value=httpx.Response(
            200,
            json=envelope(auth_payload(access_token="", refresh_token="", email=FULL_EMAIL, msisdn=FULL_PHONE)),
        )
    )
    await service.request_login_otp(email=FULL_EMAIL)
    await service.verify_login_otp(verification_pin="123456", email=FULL_EMAIL)

    result = await service.get_user_profile()

    assert result["user"]["email"] == MASKED_EMAIL
    assert result["user"]["phone"] == MASKED_PHONE


async def test_the_account_id_is_never_returned_in_full(
    service: AuthenticationService, signed_in: respx.Router
) -> None:
    await service.request_login_otp(email=FULL_EMAIL)
    await service.verify_login_otp(verification_pin="123456", email=FULL_EMAIL)

    for result in (await service.get_login_status(), await service.get_user_profile()):
        assert USER_TOKEN not in rendered(result)
        assert result["user"]["user_id"] == "b3f1...6666"


async def test_login_status_returns_only_masked_identifiers(
    service: AuthenticationService, signed_in: respx.Router
) -> None:
    await service.request_login_otp(email=FULL_EMAIL)
    await service.verify_login_otp(verification_pin="123456", email=FULL_EMAIL)

    blob = rendered(await service.get_login_status())

    assert FULL_EMAIL not in blob
    assert FULL_PHONE not in blob
    assert MASKED_EMAIL in blob


async def test_a_pending_login_reports_only_a_masked_destination(
    service: AuthenticationService, signed_in: respx.Router
) -> None:
    await service.request_login_otp(phone=FULL_PHONE)

    status = await service.get_login_status()

    assert status["pending_login"]["destination"] == MASKED_PHONE
    assert FULL_PHONE not in rendered(status)


async def test_no_tool_result_ever_contains_a_token(service: AuthenticationService, signed_in: respx.Router) -> None:
    await service.request_login_otp(email=FULL_EMAIL)
    verified = await service.verify_login_otp(verification_pin="123456", email=FULL_EMAIL)

    for result in (verified, await service.get_login_status(), await service.get_user_profile()):
        blob = rendered(result)
        assert "eyJ" not in blob, "a JWT reached a tool result"
        assert "refresh-token-value" not in blob
        assert "refresh_token" not in blob
        assert "access_token" not in blob
        assert "123456" not in blob


async def test_catalogue_results_carry_no_user_identifier_at_all(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    """The catalogue is login-free, so nothing about the user may appear in its results."""
    respx_mock.get(f"{API_URL}/bundles/countries").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
    )
    respx_mock.get(f"{API_URL}/bundles/by-country").mock(
        return_value=httpx.Response(200, json=envelope([bundle_payload()]))
    )
    respx_mock.get(f"{API_URL}/home/").mock(return_value=httpx.Response(200, json=envelope(home_payload())))

    results = [
        await catalog_service.find_bundles_by_country(country="France"),
        await catalog_service.browse_home_catalog(),
        await catalog_service.list_countries(),
    ]

    for result in results:
        blob = rendered(result).lower()
        for forbidden in ("email", "phone", "msisdn", "device_id", "user_id", "token", "session"):
            assert forbidden not in blob


# -------------------------------------------------------------------------------- logs


def emit(record_factory: Any) -> str:
    """Render one log record through the production filter and formatter."""
    record = record_factory()
    assert RedactionFilter().filter(record) is True
    return JsonLogFormatter().format(record)


def make_record(message: str, **extra: Any) -> Any:
    def factory() -> logging.LogRecord:
        record = logging.LogRecord("test", logging.INFO, __file__, 1, message, None, None)
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    return factory


def test_a_full_email_in_a_log_message_is_masked() -> None:
    output = emit(make_record(f"login for {FULL_EMAIL}"))

    assert FULL_EMAIL not in output
    assert MASKED_EMAIL in output


def test_a_full_phone_in_a_log_message_is_masked() -> None:
    output = emit(make_record(f"otp sent to {FULL_PHONE}"))

    assert FULL_PHONE not in output
    assert MASKED_PHONE in output


def test_identifiers_passed_as_structured_extras_are_masked() -> None:
    output = emit(make_record("profile_read", email=FULL_EMAIL, phone=FULL_PHONE, user_id=USER_TOKEN))

    assert FULL_EMAIL not in output
    assert FULL_PHONE not in output
    assert USER_TOKEN not in output


def test_a_device_id_and_a_token_never_survive_a_log_record() -> None:
    output = emit(make_record("backend_call", device_id="a" * 64, access_token="secret-token"))

    assert "a" * 64 not in output
    assert "secret-token" not in output


def test_a_traceback_carrying_an_identifier_is_redacted() -> None:
    def factory() -> logging.LogRecord:
        try:
            raise ValueError(f"failed for {FULL_EMAIL} / {FULL_PHONE}")
        except ValueError:
            import sys

            return logging.LogRecord("test", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())

    output = emit(factory)

    assert FULL_EMAIL not in output
    assert FULL_PHONE not in output


async def test_a_real_login_writes_no_identifier_into_the_logs(
    service: AuthenticationService, signed_in: respx.Router, caplog: pytest.LogCaptureFixture
) -> None:
    """End to end: run the flow with logging on and scan everything that was written."""
    with caplog.at_level(logging.DEBUG):
        caplog.handler.addFilter(RedactionFilter())
        await service.request_login_otp(email=FULL_EMAIL)
        await service.verify_login_otp(verification_pin="123456", email=FULL_EMAIL)
        await service.get_user_profile()

    written = "\n".join(
        [record.getMessage() for record in caplog.records] + [rendered(record.__dict__) for record in caplog.records]
    )
    assert FULL_EMAIL not in written
    assert FULL_PHONE not in written
    assert "123456" not in written
    assert USER_TOKEN not in written
