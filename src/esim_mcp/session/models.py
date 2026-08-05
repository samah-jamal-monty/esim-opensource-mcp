"""Server-side session and login-challenge records.

Neither record is ever serialized to an MCP client. Tokens are ``SecretStr`` so that
``repr``/``model_dump`` cannot leak them, and no OTP is stored in either record.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from esim_mcp.models.auth import LoginType, OtpChannel


def utc_now() -> datetime:
    """Timezone-aware current time (single source, easy to patch in tests)."""
    return datetime.now(tz=UTC)


class LoginChallenge(BaseModel):
    """A pending OTP request.

    Deliberately holds no OTP and no raw identifier -- only the masked form needed to
    tell the user which destination was used. The raw email/phone is supplied again by
    the caller at resend/verify time, because the backend contract requires it in the
    request body.
    """

    model_config = ConfigDict(extra="forbid")

    masked_identifier: str
    login_type: LoginType
    otp_channel: OtpChannel
    device_id: str
    created_at: datetime = Field(default_factory=utc_now)
    ttl_seconds: int = 300

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=self.ttl_seconds)

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utc_now()) >= self.expires_at

    def seconds_remaining(self, now: datetime | None = None) -> int:
        return max(0, int((self.expires_at - (now or utc_now())).total_seconds()))


class UserSession(BaseModel):
    """An authenticated eSIM session owned by exactly one MCP client identity."""

    model_config = ConfigDict(extra="forbid")

    session_key: str
    identity_source: str
    device_id: str
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: datetime | None = None
    user_id: str | None = None
    masked_email: str | None = None
    masked_phone: str | None = None
    currency: str | None = None
    is_verified: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def is_expired(self, now: datetime | None = None) -> bool:
        """True when the access token's own ``exp`` has already passed."""
        if self.expires_at is None:
            return False
        return (now or utc_now()) >= self.expires_at

    def needs_refresh(self, refresh_window_seconds: int, now: datetime | None = None) -> bool:
        """True when the access token expires within the configured safety window."""
        if self.expires_at is None:
            # Opaque token with no readable ``exp``: refresh reactively on 401 instead.
            return False
        return (now or utc_now()) + timedelta(seconds=refresh_window_seconds) >= self.expires_at

    def seconds_until_expiry(self, now: datetime | None = None) -> int | None:
        if self.expires_at is None:
            return None
        return max(0, int((self.expires_at - (now or utc_now())).total_seconds()))

    def safe_profile(self) -> dict[str, str | bool | None]:
        """Masked view suitable for returning from a tool."""
        from esim_mcp.safety.redaction import mask_user_id

        return {
            "user_id": mask_user_id(self.user_id),
            "email": self.masked_email,
            "phone": self.masked_phone,
            "currency": self.currency,
            "is_verified": self.is_verified,
        }
