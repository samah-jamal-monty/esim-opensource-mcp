"""Configuration validation, including production fail-fast behaviour."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from esim_mcp.settings import Environment, Settings, Transport

SALT = "a-sufficiently-long-device-id-salt-value-1234567890"


def test_base_url_is_normalized() -> None:
    settings = Settings.build(api_base_url="https://backend.test/", device_id_salt=SALT)
    assert settings.api_base_url == "https://backend.test"
    assert settings.api_v1_url == "https://backend.test/api/v1"


@pytest.mark.parametrize("value", ["", "backend.test", "ftp://backend.test", "https:///", "https://x.test?a=1"])
def test_invalid_base_url_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings.build(api_base_url=value, device_id_salt=SALT)


def test_defaults_are_safe() -> None:
    settings = Settings.build(api_base_url="https://backend.test", device_id_salt=SALT)
    assert settings.environment is Environment.LOCAL
    assert settings.transport is Transport.STDIO
    assert settings.default_currency == "USD"
    assert settings.is_production is False
    assert settings.allows_development_identity is True


def test_production_requires_https() -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings.build(api_base_url="http://backend.test", environment="production", device_id_salt=SALT)


def test_production_requires_a_salt() -> None:
    with pytest.raises(ValidationError, match="ESIM_MCP_DEVICE_ID_SALT"):
        Settings.build(api_base_url="https://backend.test", environment="production")


def test_production_rejects_short_salt() -> None:
    with pytest.raises(ValidationError, match="ESIM_MCP_DEVICE_ID_SALT"):
        Settings.build(api_base_url="https://backend.test", environment="production", device_id_salt="short")


def test_production_accepts_strong_configuration() -> None:
    settings = Settings.build(api_base_url="https://backend.test", environment="production", device_id_salt=SALT)
    assert settings.is_production is True
    assert settings.allows_development_identity is False


def test_non_production_generates_ephemeral_salt() -> None:
    settings = Settings.build(api_base_url="https://backend.test")
    assert settings.device_id_salt is not None
    assert len(settings.salt_bytes()) >= 32


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_locale", "english_locale!"),
        ("default_currency", "DOLLAR"),
        ("connect_timeout", 0),
        ("read_timeout", -1),
        ("pool_timeout", 10_000),
        ("port", 0),
        ("login_challenge_ttl_seconds", 5),
        ("token_refresh_window_seconds", -1),
        ("purchase_quote_ttl_seconds", 5),
        ("purchase_quote_ttl_seconds", 3601),
        ("max_active_quotes_per_user", 0),
        ("max_active_quotes_per_user", 500),
        ("log_level", "LOUD"),
        ("transport", "carrier-pigeon"),
        ("environment", "prod"),
    ],
)
def test_invalid_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings.build(api_base_url="https://backend.test", device_id_salt=SALT, **{field: value})


def test_environment_variables_are_read_from_prefixed_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESIM_API_BASE_URL", "https://env.backend.test")
    monkeypatch.setenv("ESIM_MCP_ENVIRONMENT", "qa")
    monkeypatch.setenv("ESIM_MCP_DEVICE_ID_SALT", SALT)
    monkeypatch.setenv("ESIM_MCP_PORT", "9100")
    monkeypatch.setenv("ESIM_MCP_TRANSPORT", "streamable-http")

    settings = Settings(_env_file=None)

    assert settings.api_base_url == "https://env.backend.test"
    assert settings.environment is Environment.QA
    assert settings.port == 9100
    assert settings.transport is Transport.STREAMABLE_HTTP


def test_unprefixed_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform-provided PORT/HOST/ENVIRONMENT must never reconfigure this server."""
    monkeypatch.setenv("ESIM_API_BASE_URL", "https://env.backend.test")
    monkeypatch.setenv("ESIM_MCP_DEVICE_ID_SALT", SALT)
    monkeypatch.setenv("PORT", "1")
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LOG_LEVEL", "CRITICAL")

    settings = Settings(_env_file=None)

    assert settings.port == 8080
    assert settings.host == "127.0.0.1"
    assert settings.environment is Environment.LOCAL
    assert settings.log_level == "INFO"


def test_purchase_quote_defaults_are_safe() -> None:
    settings = Settings.build(api_base_url="https://backend.test", device_id_salt=SALT)

    assert settings.purchase_quote_ttl_seconds == 300
    assert settings.max_active_quotes_per_user == 5


def test_purchase_quote_settings_are_read_from_their_prefixed_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESIM_API_BASE_URL", "https://env.backend.test")
    monkeypatch.setenv("ESIM_MCP_DEVICE_ID_SALT", SALT)
    monkeypatch.setenv("ESIM_MCP_PURCHASE_QUOTE_TTL_SECONDS", "600")
    monkeypatch.setenv("ESIM_MCP_MAX_ACTIVE_QUOTES_PER_USER", "3")

    settings = Settings(_env_file=None)

    assert settings.purchase_quote_ttl_seconds == 600
    assert settings.max_active_quotes_per_user == 3


def test_salt_is_not_exposed_by_repr() -> None:
    settings = Settings.build(api_base_url="https://backend.test", device_id_salt=SALT)
    assert SALT not in repr(settings)
    assert SALT not in str(settings.device_id_salt)
