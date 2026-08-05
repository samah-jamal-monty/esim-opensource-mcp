"""Masking and redaction primitives.

Two separate jobs live here:

* **Masking** (:func:`mask_email`, :func:`mask_phone`, ...) produces the deliberately
  reduced values that tools are allowed to *return* to MCP clients.
* **Redaction** (:func:`redact_text`, :func:`redact_mapping`) is a last line of defence
  applied to everything that reaches a log record, so a secret that slips into an
  f-string never lands in a log file.

Redaction is intentionally aggressive: it prefers destroying an innocent value over
leaking a token, an OTP or an ICCID.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"

#: Keys whose values are replaced wholesale.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "activation_code",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "device_id",
        "iccid",
        "jwt",
        "matching_id",
        "otp",
        "password",
        "pin",
        "refresh_token",
        "salt",
        "secret",
        "session_id",
        "session_key",
        "smdp_address",
        "token",
        "user_token",
        "verification_pin",
        "x-api-key",
        "x-device-id",
        "x-refresh-token",
    }
)

#: Keys whose values are masked rather than destroyed, so logs stay useful.
MASKED_KEYS: frozenset[str] = frozenset(
    {
        "destination",
        "email",
        "mobile",
        "msisdn",
        "phone",
        "phone_number",
        "user_email",
        "user_id",
    }
)

_ACTIVATION_CODE_RE = re.compile(r"LPA:1\$[^\s\"',}\]]+", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{6,}")
_KEY_VALUE_KEYS = sorted(
    SENSITIVE_KEYS | {"x_refresh_token", "x_device_id", "verification-pin"},
    key=len,
    reverse=True,
)
_KEY_VALUE_RE = re.compile(
    r"(?i)\b(?P<key>"
    + "|".join(re.escape(key) for key in _KEY_VALUE_KEYS)
    + r")(?P<sep>[\"']?\s*[:=]\s*[\"']?)(?P<value>[^\s,;\"'}\)\]]+)"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\+\d[\d\s().-]{5,20}\d")
_ICCID_RE = re.compile(r"\b\d{15,22}\b")
_OTP_RE = re.compile(r"\b\d{6}\b")


def mask_email(value: str | None) -> str | None:
    """``mohammad@example.com`` -> ``m***@example.com``."""
    if not value:
        return None
    local, separator, domain = value.partition("@")
    if not separator or not local:
        return mask_identifier(value)
    return f"{local[0]}***@{domain}" if domain else f"{local[0]}***"


def mask_phone(value: str | None) -> str | None:
    """``+96171123467`` -> ``+961******67`` (country hint and last two digits only)."""
    if not value:
        return None
    stripped = value.strip()
    prefix = "+" if stripped.startswith("+") else ""
    digits = re.sub(r"\D", "", stripped)
    if len(digits) <= 4:
        return f"{prefix}{'*' * len(digits)}"
    head = digits[:3]
    tail = digits[-2:]
    return f"{prefix}{head}{'*' * (len(digits) - 5)}{tail}"


def mask_identifier(value: str | None) -> str | None:
    """Mask an identifier of unknown shape, keeping only its first character."""
    if not value:
        return None
    if "@" in value:
        return mask_email(value)
    if value.strip().startswith("+"):
        return mask_phone(value)
    if len(value) <= 2:
        return "*" * len(value)
    return f"{value[0]}***{value[-1]}"


def mask_user_id(value: str | None) -> str | None:
    """Keep only the first and last four characters of an opaque user id."""
    if not value:
        return None
    if len(value) <= 10:
        return f"{value[0]}***"
    return f"{value[:4]}...{value[-4:]}"


def redact_text(value: str) -> str:
    """Remove secrets from a rendered string.

    Ordering matters: longer/structured secrets are handled before generic digit runs so
    a JWT or an ICCID is not partially rewritten by a narrower rule.
    """
    if not value:
        return value
    text = _ACTIVATION_CODE_RE.sub(REDACTED, value)
    text = _JWT_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub(rf"\1 {REDACTED}", text)
    text = _KEY_VALUE_RE.sub(lambda match: f"{match.group('key')}{match.group('sep')}{REDACTED}", text)
    text = _EMAIL_RE.sub(lambda match: mask_email(match.group(0)) or REDACTED, text)
    text = _PHONE_RE.sub(lambda match: mask_phone(match.group(0)) or REDACTED, text)
    text = _ICCID_RE.sub(REDACTED, text)
    return _OTP_RE.sub(REDACTED, text)


def redact_mapping(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure.

    Sensitive keys are replaced, identifier-like keys are masked, and every remaining
    string still goes through :func:`redact_text`.
    """
    if _depth > 12:
        return REDACTED
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.strip().lower().replace("-", "_")
            if normalized in SENSITIVE_KEYS or normalized.replace("_", "-") in SENSITIVE_KEYS:
                result[key] = REDACTED
            elif normalized in MASKED_KEYS:
                result[key] = mask_identifier(item) if isinstance(item, str) else REDACTED
            else:
                result[key] = redact_mapping(item, _depth=_depth + 1)
        return result
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [redact_mapping(item, _depth=_depth + 1) for item in value]
    if isinstance(value, bytes | bytearray):
        return REDACTED
    return value
