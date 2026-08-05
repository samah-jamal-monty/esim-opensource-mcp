"""Authentication models mirroring the backend contract.

Only the fields the MCP server actually needs are modelled. Secrets are held as
``SecretStr`` so that a stray ``repr()``/``model_dump()`` cannot spill them, and the OTP
is never stored anywhere beyond the lifetime of a single verify call.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from esim_mcp.errors import InvalidInputError

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^\+\d{6,17}$")
_OTP_RE = re.compile(r"^\d{6}$")


class OtpChannel(StrEnum):
    """Channel the backend should deliver the OTP through."""

    EMAIL = "EMAIL"
    SMS = "SMS"


class LoginType(StrEnum):
    """Which identifier the login challenge was created for."""

    EMAIL = "email"
    PHONE = "phone"


def _normalize_email(value: str) -> str:
    candidate = value.strip()
    if not _EMAIL_RE.match(candidate):
        raise ValueError("email must be a valid address")
    if "+" in candidate.split("@", 1)[0]:
        # The backend rejects sub-addressed locals outright; fail before the round trip.
        raise ValueError("email must not contain '+' in the local part")
    return candidate


def _normalize_phone(value: str) -> str:
    candidate = re.sub(r"[\s()-]", "", value.strip())
    if not _PHONE_RE.match(candidate):
        raise ValueError("phone must be in international format, e.g. +CCXXXXXXXXX")
    return candidate


class LoginRequest(BaseModel):
    """``POST /api/v1/auth/login`` and ``POST /api/v1/auth/resend-otp`` body."""

    model_config = ConfigDict(extra="forbid")

    email: str | None = None
    phone: str | None = None
    otp_channel: OtpChannel | None = None

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str | None) -> str | None:
        return _normalize_email(value) if value else None

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value: str | None) -> str | None:
        return _normalize_phone(value) if value else None

    @model_validator(mode="after")
    def _require_identifier(self) -> LoginRequest:
        if not self.email and not self.phone:
            raise ValueError("either email or phone is required")
        return self

    @property
    def login_type(self) -> LoginType:
        """The backend prefers phone whenever one is present."""
        return LoginType.PHONE if self.phone else LoginType.EMAIL

    @property
    def identifier(self) -> str:
        value = self.phone or self.email
        assert value is not None  # guaranteed by _require_identifier
        return value

    def resolved_channel(self) -> OtpChannel:
        """Default the channel from the identifier when the caller did not pick one."""
        if self.otp_channel is not None:
            return self.otp_channel
        return OtpChannel.SMS if self.phone else OtpChannel.EMAIL

    def to_payload(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "phone": self.phone,
            "otp_channel": self.resolved_channel().value,
        }


class VerifyOtpRequest(BaseModel):
    """``POST /api/v1/auth/verify_otp`` body. The PIN is never persisted or logged."""

    model_config = ConfigDict(extra="forbid")

    user_email: str | None = None
    phone: str | None = None
    verification_pin: SecretStr

    @field_validator("user_email")
    @classmethod
    def _check_email(cls, value: str | None) -> str | None:
        return _normalize_email(value) if value else None

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value: str | None) -> str | None:
        return _normalize_phone(value) if value else None

    @field_validator("verification_pin")
    @classmethod
    def _check_pin(cls, value: SecretStr) -> SecretStr:
        if not _OTP_RE.match(value.get_secret_value().strip()):
            raise ValueError("verification_pin must be exactly six digits")
        return SecretStr(value.get_secret_value().strip())

    @model_validator(mode="after")
    def _require_identifier(self) -> VerifyOtpRequest:
        if not self.user_email and not self.phone:
            raise ValueError("either user_email or phone is required")
        return self

    @property
    def identifier(self) -> str:
        value = self.phone or self.user_email
        assert value is not None  # guaranteed by _require_identifier
        return value

    def to_payload(self) -> dict[str, Any]:
        """Build the wire body. The returned dict must never be logged."""
        return {
            "user_email": self.user_email,
            "phone": self.phone,
            "verification_pin": self.verification_pin.get_secret_value(),
        }


class UserInfo(BaseModel):
    """Subset of the backend ``user_info`` object that Phase 1 needs."""

    model_config = ConfigDict(extra="ignore")

    is_verified: bool = False
    email: str | None = None
    msisdn: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    currency_code: str | None = None
    balance: float | None = None
    country: str | None = None
    country_code: str | None = None
    language: str | None = None
    referral_code: str | None = None
    role_name: str | None = None


class BackendAuthResponse(BaseModel):
    """``AuthResponseDTO`` as returned inside the envelope's ``data``.

    ``GET /auth/user-info`` reuses this DTO but answers with empty token strings, so the
    token fields are optional here and validated explicitly where real tokens are
    required (OTP verification and refresh-token rotation).

    ``user_token`` is the backend's opaque user identifier. It never leaves the process
    unmasked.
    """

    model_config = ConfigDict(extra="ignore")

    access_token: SecretStr = SecretStr("")
    refresh_token: SecretStr = SecretStr("")
    user_token: str | None = None
    is_verified: bool = False
    user_info: UserInfo = Field(default_factory=UserInfo)

    @property
    def has_session_tokens(self) -> bool:
        """True when the payload carries a usable access/refresh token pair."""
        return bool(self.access_token.get_secret_value()) and bool(self.refresh_token.get_secret_value())

    def access_token_expiry(self) -> datetime | None:
        """Expiry read from the access token's unverified ``exp`` claim."""
        return decode_unverified_jwt_expiry(self.access_token.get_secret_value())


def decode_unverified_jwt_expiry(token: str) -> datetime | None:
    """Read ``exp`` from a JWT **without verifying its signature**.

    This is a scheduling aid for proactive refresh only. The MCP server performs no
    authorization on the strength of this value -- the eSIM backend remains the sole
    authority for validating tokens. Returns ``None`` when the token is opaque or has no
    usable ``exp``.
    """
    if not token or token.count(".") != 2:
        return None
    payload_segment = token.split(".")[1]
    padding = "=" * (-len(payload_segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_segment + padding)
        claims = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if not isinstance(exp, int | float) or isinstance(exp, bool):
        return None
    try:
        return datetime.fromtimestamp(float(exp), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def build_login_request(
    email: str | None,
    phone: str | None,
    otp_channel: str | None,
) -> LoginRequest:
    """Validate raw tool arguments into a :class:`LoginRequest`.

    Pydantic validation errors are converted into a safe :class:`InvalidInputError`; the
    raw error text is not echoed because it embeds the offending value.
    """
    channel: OtpChannel | None
    if otp_channel is None:
        channel = None
    else:
        try:
            channel = OtpChannel(otp_channel.strip().upper())
        except ValueError:
            raise InvalidInputError("otp_channel must be either 'EMAIL' or 'SMS'.") from None
    if not (email or "").strip() and not (phone or "").strip():
        # Actionable for an AI caller: it says what to do next instead of naming a field.
        raise InvalidInputError(
            "No email address or phone number was supplied. Ask the user which email address or "
            "phone number to send the verification code to, then call this tool again."
        )
    try:
        return LoginRequest(email=email or None, phone=phone or None, otp_channel=channel)
    except ValueError as exc:
        raise InvalidInputError(_first_safe_message(exc, "Provide a valid email address or phone number.")) from None


def build_verify_request(email: str | None, phone: str | None, verification_pin: str) -> VerifyOtpRequest:
    """Validate raw tool arguments into a :class:`VerifyOtpRequest`."""
    if not (email or "").strip() and not (phone or "").strip():
        raise InvalidInputError(
            "No email address or phone number was supplied. Pass the same email address or phone "
            "number the verification code was sent to."
        )
    try:
        return VerifyOtpRequest(
            user_email=email or None,
            phone=phone or None,
            verification_pin=SecretStr(verification_pin or ""),
        )
    except ValueError as exc:
        raise InvalidInputError(_first_safe_message(exc, "The verification request is invalid.")) from None


_SAFE_VALIDATION_MESSAGES = {
    "email must be a valid address": "Provide a valid email address.",
    "email must not contain '+' in the local part": "The email address must not contain '+' before the '@'.",
    "phone must be in international format, e.g. +CCXXXXXXXXX": (
        "Provide the phone number in international format, for example +CCXXXXXXXXX."
    ),
    "verification_pin must be exactly six digits": "The verification code must be exactly six digits.",
    "either email or phone is required": "Provide either an email address or a phone number.",
    "either user_email or phone is required": "Provide either an email address or a phone number.",
}


def _first_safe_message(exc: Exception, fallback: str) -> str:
    """Map a pydantic validation failure onto a message that contains no user data."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        for error in errors():
            raw = str(error.get("msg", "")).removeprefix("Value error, ").strip()
            if raw in _SAFE_VALIDATION_MESSAGES:
                return _SAFE_VALIDATION_MESSAGES[raw]
    return fallback
