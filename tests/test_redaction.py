"""Masking, redaction and the guarantee that secrets never reach a rendered log."""

from __future__ import annotations

import io
import logging

import pytest

from esim_mcp.logging_config import (
    JsonLogFormatter,
    RedactionFilter,
    correlation_context,
    get_correlation_id,
)
from esim_mcp.safety.redaction import (
    REDACTED,
    mask_email,
    mask_identifier,
    mask_phone,
    mask_user_id,
    redact_mapping,
    redact_text,
)
from tests.conftest import make_jwt

ACCESS_TOKEN = make_jwt(subject="secret-subject")
REFRESH_TOKEN = "v1.MnQ4c2VjcmV0LXJlZnJlc2gtdG9rZW4"
OTP = "483920"
EMAIL = "mohammad@example.com"
PHONE = "+96171123467"
ICCID = "8944500123456789012"
DEVICE_ID = "9f2c" * 16


def test_mask_email() -> None:
    assert mask_email(EMAIL) == "m***@example.com"
    assert mask_email(None) is None


def test_mask_phone_keeps_only_country_hint_and_last_digits() -> None:
    masked = mask_phone(PHONE)
    assert masked is not None
    assert masked.startswith("+961")
    assert masked.endswith("67")
    assert "7112346" not in masked


def test_mask_identifier_dispatches_by_shape() -> None:
    assert mask_identifier(EMAIL) == "m***@example.com"
    assert mask_identifier(PHONE) == mask_phone(PHONE)
    assert mask_identifier("opaque-value") == "o***e"


def test_mask_user_id() -> None:
    assert mask_user_id("b3f1c0de-1111-2222-3333-444455556666") == "b3f1...6666"
    assert mask_user_id(None) is None


@pytest.mark.parametrize(
    "secret",
    [ACCESS_TOKEN, OTP, ICCID, "LPA:1$rsp.example.test$K2-1A2B3C"],
)
def test_redact_text_removes_secrets(secret: str) -> None:
    rendered = redact_text(f"payload contains {secret} inline")
    assert secret not in rendered
    assert REDACTED in rendered


def test_redact_text_handles_headers_and_key_values() -> None:
    rendered = redact_text(
        f"Authorization: Bearer {ACCESS_TOKEN} x-refresh-token={REFRESH_TOKEN} "
        f"verification_pin={OTP} device_id={DEVICE_ID}"
    )
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, OTP, DEVICE_ID):
        assert secret not in rendered


def test_redact_text_masks_contact_details() -> None:
    rendered = redact_text(f"login for {EMAIL} / {PHONE}")
    assert EMAIL not in rendered
    assert PHONE not in rendered
    assert "m***@example.com" in rendered


def test_redact_mapping_is_recursive() -> None:
    payload = {
        "access_token": ACCESS_TOKEN,
        "user": {"email": EMAIL, "msisdn": PHONE, "verification_pin": OTP},
        "items": [{"iccid": ICCID}, f"raw {ACCESS_TOKEN}"],
        "responseCode": 200,
    }

    redacted = redact_mapping(payload)
    flattened = repr(redacted)

    for secret in (ACCESS_TOKEN, EMAIL, PHONE, OTP, ICCID):
        assert secret not in flattened
    assert redacted["responseCode"] == 200


def test_correlation_context_is_request_local() -> None:
    assert get_correlation_id() is None
    with correlation_context() as correlation_id:
        assert get_correlation_id() == correlation_id
        assert correlation_id.isalnum()
    assert get_correlation_id() is None


@pytest.fixture
def captured_log() -> tuple[logging.Logger, io.StringIO, logging.Handler]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger("esim_mcp.test.redaction")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, stream, handler


def test_secrets_never_appear_in_rendered_logs(
    captured_log: tuple[logging.Logger, io.StringIO, logging.Handler],
) -> None:
    logger, stream, _ = captured_log

    with correlation_context("abc123def456"):
        logger.info("login attempt for %s with pin %s", EMAIL, OTP)
        logger.info(
            "backend call",
            extra={"authorization": f"Bearer {ACCESS_TOKEN}", "device_id": DEVICE_ID, "phone": PHONE},
        )
        logger.debug("refresh token rotated refresh_token=%s", REFRESH_TOKEN)

    output = stream.getvalue()

    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, OTP, EMAIL, PHONE, DEVICE_ID):
        assert secret not in output
    assert "abc123def456" in output


def test_tracebacks_are_redacted_before_reaching_a_handler(
    captured_log: tuple[logging.Logger, io.StringIO, logging.Handler],
) -> None:
    logger, stream, _ = captured_log

    try:
        raise ValueError(f"unexpected payload verification_pin={OTP} token={ACCESS_TOKEN}")
    except ValueError:
        logger.exception("tool failed")

    output = stream.getvalue()
    assert OTP not in output
    assert ACCESS_TOKEN not in output
    assert "ValueError" in output
