"""Typed configuration for the eSIM MCP server.

Every value is read from the environment (optionally seeded by a local, git-ignored
``.env`` file). Unsafe configuration fails fast when ``ESIM_MCP_ENVIRONMENT`` is
``production`` -- the server never silently degrades to an insecure default there.
"""

from __future__ import annotations

import logging
import re
import secrets
from enum import StrEnum
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

#: Every backend route lives under this prefix.
API_V1_PREFIX = "/api/v1"

#: Shortest device-id salt accepted in production.
MIN_DEVICE_ID_SALT_LENGTH = 32

_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?$")
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Environment(StrEnum):
    """Deployment environment. Only ``PRODUCTION`` enables fail-closed mode."""

    LOCAL = "local"
    DEVELOPMENT = "development"
    QA = "qa"
    STAGING = "staging"
    PRODUCTION = "production"


class Transport(StrEnum):
    """MCP transports supported by the installed SDK and by this server."""

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"


class Settings(BaseSettings):
    """Runtime configuration.

    Environment variables are read from the explicit aliases only. ``populate_by_name``
    is deliberately off: with it enabled pydantic-settings would additionally read bare
    field names from the environment, so an unrelated ``PORT``, ``HOST``, ``ENVIRONMENT``
    or ``LOG_LEVEL`` set by a platform would silently reconfigure this server. Use
    :meth:`Settings.build` to construct instances from field names in code and tests.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=False,
        case_sensitive=False,
    )

    api_base_url: str = Field(validation_alias="ESIM_API_BASE_URL")
    environment: Environment = Field(default=Environment.LOCAL, validation_alias="ESIM_MCP_ENVIRONMENT")
    transport: Transport = Field(default=Transport.STDIO, validation_alias="ESIM_MCP_TRANSPORT")
    host: str = Field(default="127.0.0.1", validation_alias="ESIM_MCP_HOST")
    port: int = Field(default=8080, ge=1, le=65535, validation_alias="ESIM_MCP_PORT")
    device_id_salt: SecretStr | None = Field(default=None, validation_alias="ESIM_MCP_DEVICE_ID_SALT")
    default_locale: str = Field(default="en", validation_alias="ESIM_MCP_DEFAULT_LOCALE")
    default_currency: str = Field(default="USD", validation_alias="ESIM_MCP_DEFAULT_CURRENCY")
    connect_timeout: float = Field(default=5.0, gt=0, le=300, validation_alias="ESIM_MCP_CONNECT_TIMEOUT")
    read_timeout: float = Field(default=20.0, gt=0, le=300, validation_alias="ESIM_MCP_READ_TIMEOUT")
    #: Read budget for the card-checkout POST specifically, which is the slowest call this
    #: server makes by a wide margin: the platform's checkout endpoint performs up to six
    #: sequential third-party round trips (two eSIM-hub reads, two order-row writes, a
    #: currency rate and the Stripe Checkout Session) before it can answer. Every other route
    #: this server calls is a single cached read, so :attr:`read_timeout` is sized for those.
    #:
    #: Giving up early here is not a neutral failure. The platform finishes the work anyway
    #: and a real hosted payment page comes into existence, but this server never reads the
    #: link or the payment reference and has no way to ask for either afterwards -- so a
    #: successful checkout is reported to the user as "the payment page could not be opened".
    #: That asymmetry is why this budget is generous rather than tight.
    checkout_read_timeout: float = Field(
        default=45.0, gt=0, le=300, validation_alias="ESIM_MCP_CHECKOUT_READ_TIMEOUT"
    )
    #: Read budget for the wallet-purchase POST. Larger than every other budget here, and for
    #: a harder reason than the card one.
    #:
    #: That route delegates to the platform's shared assign flow, which debits the wallet
    #: *and then provisions an eSIM against the eSIM hub before it answers*. A measured run
    #: took 65 seconds end to end: order row at +8s, wallet debited at +10s, eSIM issued at
    #: +59s, response at +65s. Nothing about that is pathological -- issuing a SIM profile is
    #: simply slow.
    #:
    #: Abandoning that request is worse than abandoning a checkout, and not by a little. The
    #: platform finishes anyway: the money leaves the wallet, the eSIM is issued, the order
    #: closes -- and this server, having hung up, can only report that it does not know
    #: whether the user was charged. Worse, the platform does not honour the
    #: ``Idempotency-Key`` on that route, so the "just try again" that an unknown outcome
    #: invites can buy the plan a second time. Every one of those failures is bought by a
    #: timeout that fired while the purchase was succeeding.
    purchase_read_timeout: float = Field(
        default=90.0, gt=0, le=300, validation_alias="ESIM_MCP_PURCHASE_READ_TIMEOUT"
    )
    write_timeout: float = Field(default=20.0, gt=0, le=300, validation_alias="ESIM_MCP_WRITE_TIMEOUT")
    pool_timeout: float = Field(default=5.0, gt=0, le=300, validation_alias="ESIM_MCP_POOL_TIMEOUT")
    token_refresh_window_seconds: int = Field(
        default=120, ge=0, le=3600, validation_alias="ESIM_MCP_TOKEN_REFRESH_WINDOW_SECONDS"
    )
    login_challenge_ttl_seconds: int = Field(
        default=300, ge=30, le=1800, validation_alias="ESIM_MCP_LOGIN_CHALLENGE_TTL_SECONDS"
    )
    #: How long a prepared purchase quote stays usable. Short on purpose: a quote carries a
    #: catalogue price and a wallet balance, and both go stale.
    purchase_quote_ttl_seconds: int = Field(
        default=300, ge=30, le=1800, validation_alias="ESIM_MCP_PURCHASE_QUOTE_TTL_SECONDS"
    )
    #: Upper bound on simultaneously active quotes per owner, so a looping caller cannot
    #: grow the store without limit.
    max_active_quotes_per_user: int = Field(
        default=5, ge=1, le=50, validation_alias="ESIM_MCP_MAX_ACTIVE_QUOTES_PER_USER"
    )
    log_level: LogLevel = Field(default="INFO", validation_alias="ESIM_MCP_LOG_LEVEL")

    #: Identity handed to stdio/local callers when no authenticated transport principal
    #: exists. Never accepted in production.
    dev_client_id: str = Field(default="local-dev-client", validation_alias="ESIM_MCP_DEV_CLIENT_ID")

    @field_validator("api_base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        candidate = value.strip().rstrip("/")
        if not candidate:
            raise ValueError("ESIM_API_BASE_URL must not be empty")
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("ESIM_API_BASE_URL must start with http:// or https://")
        if not parsed.netloc:
            raise ValueError("ESIM_API_BASE_URL must include a host")
        if parsed.query or parsed.fragment:
            raise ValueError("ESIM_API_BASE_URL must not contain a query string or fragment")
        return candidate

    @field_validator("default_locale")
    @classmethod
    def _validate_locale(cls, value: str) -> str:
        candidate = value.strip()
        if not _LOCALE_RE.match(candidate):
            raise ValueError("ESIM_MCP_DEFAULT_LOCALE must look like 'en' or 'en-US'")
        return candidate

    @field_validator("default_currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        candidate = value.strip().upper()
        if not _CURRENCY_RE.match(candidate):
            raise ValueError("ESIM_MCP_DEFAULT_CURRENCY must be a 3-letter ISO 4217 code")
        return candidate

    @field_validator("dev_client_id")
    @classmethod
    def _validate_dev_client_id(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("ESIM_MCP_DEV_CLIENT_ID must not be empty")
        return candidate

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Settings:
        if self.is_production:
            if not self.api_base_url.startswith("https://"):
                raise ValueError("ESIM_API_BASE_URL must use https:// in production")
            salt = self.device_id_salt.get_secret_value() if self.device_id_salt else ""
            if len(salt) < MIN_DEVICE_ID_SALT_LENGTH:
                raise ValueError(
                    "ESIM_MCP_DEVICE_ID_SALT is required in production and must be at least "
                    f"{MIN_DEVICE_ID_SALT_LENGTH} characters"
                )
            return self

        if self.device_id_salt is None or not self.device_id_salt.get_secret_value():
            # Non-production convenience only: device ids stay stable for the lifetime of
            # the process but change on restart. Never reached in production.
            object.__setattr__(self, "device_id_salt", SecretStr(secrets.token_urlsafe(48)))
            logger.warning(
                "ESIM_MCP_DEVICE_ID_SALT is not set; generated an ephemeral salt for this process. "
                "Device ids will change on restart. Configure a persistent salt for stable device ids."
            )
        return self

    @classmethod
    def build(cls, *, use_env_file: bool = False, **values: object) -> Settings:
        """Construct settings from *field names* instead of environment aliases.

        Keeps call sites and tests readable without enabling ``populate_by_name``
        (which would make pydantic-settings read bare field names from the environment).
        """
        aliases = {
            name: (field.validation_alias if isinstance(field.validation_alias, str) else name)
            for name, field in cls.model_fields.items()
        }
        payload = {aliases.get(key, key): value for key, value in values.items()}
        env_file = ".env" if use_env_file else None
        return cls(_env_file=env_file, **payload)  # type: ignore[arg-type]

    @property
    def is_production(self) -> bool:
        """True when fail-closed production behaviour is required."""
        return self.environment is Environment.PRODUCTION

    @property
    def allows_development_identity(self) -> bool:
        """True when unauthenticated (stdio/local) callers may use the development identity."""
        return not self.is_production

    @property
    def api_v1_url(self) -> str:
        """Base URL including the ``/api/v1`` prefix, without a trailing slash."""
        return f"{self.api_base_url}{API_V1_PREFIX}"

    def salt_bytes(self) -> bytes:
        """The device-id salt as bytes. The salt itself is never logged."""
        assert self.device_id_salt is not None  # guaranteed by _enforce_production_safety
        return self.device_id_salt.get_secret_value().encode("utf-8")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings (used by tests and by re-configuration)."""
    get_settings.cache_clear()
