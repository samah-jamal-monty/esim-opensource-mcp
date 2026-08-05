"""Identity, device-id derivation, session isolation and token-refresh behaviour."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from esim_mcp.client.auth import AuthApiClient
from esim_mcp.errors import AuthenticationRequiredError, IdentityUnavailableError
from esim_mcp.models.auth import BackendAuthResponse
from esim_mcp.session.identity import (
    AuthenticatedTransportIdentityProvider,
    ClientIdentity,
    DevelopmentIdentityProvider,
    ResolvingClientIdentityProvider,
    derive_device_id,
)
from esim_mcp.session.manager import SessionManager
from esim_mcp.session.store import InMemorySessionStore
from esim_mcp.settings import Settings
from tests.conftest import API_URL, TEST_SALT, auth_payload, envelope, make_jwt

SALT = TEST_SALT.encode()


# --------------------------------------------------------------------------- identity


def test_device_id_is_stable_for_the_same_identity_and_salt() -> None:
    identity = ClientIdentity(value="client-a", source="test")

    first = derive_device_id(SALT, identity)
    second = derive_device_id(SALT, identity)

    assert first == second
    assert len(first) == 64
    assert first != identity.value
    assert "client-a" not in first


def test_different_clients_get_different_device_ids() -> None:
    a = derive_device_id(SALT, ClientIdentity(value="client-a", source="test"))
    b = derive_device_id(SALT, ClientIdentity(value="client-b", source="test"))

    assert a != b


def test_device_id_changes_with_the_salt() -> None:
    identity = ClientIdentity(value="client-a", source="test")

    assert derive_device_id(SALT, identity) != derive_device_id(b"another-salt-value-0123456789", identity)


def test_session_keys_are_isolated_and_non_reversible() -> None:
    a = ClientIdentity(value="client-a", source="test")
    b = ClientIdentity(value="client-b", source="test")

    assert a.session_key != b.session_key
    assert "client-a" not in a.session_key
    assert "client-a" not in repr(a)


async def test_authenticated_provider_fails_closed_without_a_principal() -> None:
    with pytest.raises(IdentityUnavailableError):
        await AuthenticatedTransportIdentityProvider().resolve(None)


async def test_production_never_falls_back_to_the_development_identity() -> None:
    settings = Settings.build(api_base_url="https://backend.test", environment="production", device_id_salt=TEST_SALT)

    with pytest.raises(IdentityUnavailableError):
        DevelopmentIdentityProvider(settings)

    with pytest.raises(IdentityUnavailableError):
        await ResolvingClientIdentityProvider(settings).resolve(None)


async def test_development_identity_is_stable_outside_production(settings: Settings) -> None:
    provider = ResolvingClientIdentityProvider(settings)

    first = await provider.resolve(None)
    second = await provider.resolve(None)

    assert first.session_key == second.session_key
    assert derive_device_id(settings.salt_bytes(), first) == derive_device_id(settings.salt_bytes(), second)


# ---------------------------------------------------------------------------- sessions


async def make_session(
    manager: SessionManager,
    identity: ClientIdentity,
    *,
    expires_in: int = 3600,
    access_token: str | None = None,
    email: str = "person@example.com",
) -> None:
    auth = BackendAuthResponse.model_validate(
        auth_payload(
            access_token=access_token or make_jwt(expires_in=expires_in, subject=identity.value),
            email=email,
        )
    )
    await manager.create_session(identity, device_id="device-1", auth=auth, currency="USD")


async def test_one_client_cannot_read_or_overwrite_another_clients_session(
    session_manager: SessionManager,
) -> None:
    user_a = ClientIdentity(value="client-a", source="test")
    user_b = ClientIdentity(value="client-b", source="test")

    await make_session(session_manager, user_a, email="alice@example.com")
    await make_session(session_manager, user_b, email="bob@example.com")

    session_a = await session_manager.require_session(user_a.session_key)
    session_b = await session_manager.require_session(user_b.session_key)

    assert session_a.masked_email == "a***@example.com"
    assert session_b.masked_email == "b***@example.com"
    assert session_a.access_token.get_secret_value() != session_b.access_token.get_secret_value()

    await session_manager.invalidate(user_a.session_key)

    assert await session_manager.get_session(user_a.session_key) is None
    assert await session_manager.get_session(user_b.session_key) is not None


async def test_missing_session_raises_authentication_required(session_manager: SessionManager) -> None:
    with pytest.raises(AuthenticationRequiredError):
        await session_manager.require_session("unknown-key")


async def test_expiring_token_is_refreshed_proactively(
    session_manager: SessionManager, respx_mock: respx.Router
) -> None:
    identity = ClientIdentity(value="client-a", source="test")
    await make_session(session_manager, identity, expires_in=30)  # inside the 60s window
    rotated = make_jwt(expires_in=3600, signature="rotated")
    route = respx_mock.post(f"{API_URL}/auth/refresh-token").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=rotated)))
    )

    session = await session_manager.ensure_fresh_session(
        identity.session_key, device_id="device-1", locale="en", currency="USD"
    )

    assert route.call_count == 1
    assert session.access_token.get_secret_value() == rotated
    assert session.expires_at is not None and session.expires_at > datetime.now(tz=UTC) + timedelta(minutes=50)


async def test_fresh_token_is_not_refreshed(session_manager: SessionManager, respx_mock: respx.Router) -> None:
    identity = ClientIdentity(value="client-a", source="test")
    await make_session(session_manager, identity, expires_in=3600)
    route = respx_mock.post(f"{API_URL}/auth/refresh-token").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload()))
    )

    await session_manager.ensure_fresh_session(identity.session_key, device_id="device-1", locale="en", currency="USD")

    assert route.call_count == 0


async def test_concurrent_refreshes_produce_a_single_backend_call(
    session_manager: SessionManager, respx_mock: respx.Router
) -> None:
    identity = ClientIdentity(value="client-a", source="test")
    await make_session(session_manager, identity, expires_in=10)
    route = respx_mock.post(f"{API_URL}/auth/refresh-token").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=make_jwt(expires_in=3600))))
    )

    results = await asyncio.gather(
        *(
            session_manager.ensure_fresh_session(
                identity.session_key, device_id="device-1", locale="en", currency="USD"
            )
            for _ in range(8)
        )
    )

    assert route.call_count == 1
    assert len({session.access_token.get_secret_value() for session in results}) == 1


async def test_failed_refresh_invalidates_the_session(
    session_manager: SessionManager, respx_mock: respx.Router
) -> None:
    identity = ClientIdentity(value="client-a", source="test")
    await make_session(session_manager, identity, expires_in=10)
    route = respx_mock.post(f"{API_URL}/auth/refresh-token").mock(
        return_value=httpx.Response(401, json=envelope(None, status="failed", response_code=401))
    )

    with pytest.raises(AuthenticationRequiredError):
        await session_manager.ensure_fresh_session(
            identity.session_key, device_id="device-1", locale="en", currency="USD"
        )

    assert route.call_count == 1
    assert await session_manager.get_session(identity.session_key) is None


async def test_401_triggers_one_refresh_and_one_replay(
    session_manager: SessionManager, respx_mock: respx.Router
) -> None:
    identity = ClientIdentity(value="client-a", source="test")
    await make_session(session_manager, identity, expires_in=3600)
    rotated = make_jwt(expires_in=3600, signature="rotated")
    refresh_route = respx_mock.post(f"{API_URL}/auth/refresh-token").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=rotated)))
    )
    attempts: list[str] = []

    async def operation(token: object) -> str:
        attempts.append(str(token))
        if len(attempts) == 1:
            raise AuthenticationRequiredError()
        return "ok"

    result = await session_manager.run_authenticated(
        identity.session_key, operation, device_id="device-1", locale="en", currency="USD"
    )

    assert result == "ok"
    assert refresh_route.call_count == 1
    assert len(attempts) == 2


async def test_replay_is_attempted_only_once(session_manager: SessionManager, respx_mock: respx.Router) -> None:
    identity = ClientIdentity(value="client-a", source="test")
    await make_session(session_manager, identity, expires_in=3600)
    respx_mock.post(f"{API_URL}/auth/refresh-token").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=make_jwt(signature="rotated"))))
    )
    attempts = 0

    async def always_unauthorized(token: object) -> str:
        nonlocal attempts
        attempts += 1
        raise AuthenticationRequiredError()

    with pytest.raises(AuthenticationRequiredError):
        await session_manager.run_authenticated(
            identity.session_key, always_unauthorized, device_id="device-1", locale="en", currency="USD"
        )

    assert attempts == 2
    assert await session_manager.get_session(identity.session_key) is None


async def test_replay_can_be_disabled_for_unsafe_operations(
    session_manager: SessionManager, respx_mock: respx.Router
) -> None:
    identity = ClientIdentity(value="client-a", source="test")
    await make_session(session_manager, identity, expires_in=3600)
    refresh_route = respx_mock.post(f"{API_URL}/auth/refresh-token")
    attempts = 0

    async def operation(token: object) -> str:
        nonlocal attempts
        attempts += 1
        raise AuthenticationRequiredError()

    with pytest.raises(AuthenticationRequiredError):
        await session_manager.run_authenticated(
            identity.session_key,
            operation,
            device_id="device-1",
            locale="en",
            currency="USD",
            allow_refresh_replay=False,
        )

    assert attempts == 1
    assert refresh_route.call_count == 0


async def test_logout_removes_only_the_calling_clients_session(
    session_manager: SessionManager, respx_mock: respx.Router
) -> None:
    user_a = ClientIdentity(value="client-a", source="test")
    user_b = ClientIdentity(value="client-b", source="test")
    await make_session(session_manager, user_a)
    await make_session(session_manager, user_b)
    respx_mock.post(f"{API_URL}/auth/logout").mock(return_value=httpx.Response(200, json=envelope(None)))

    acknowledged = await session_manager.logout(user_a.session_key, device_id="device-1", locale="en")

    assert acknowledged is True
    assert await session_manager.get_session(user_a.session_key) is None
    assert await session_manager.get_session(user_b.session_key) is not None


async def test_logout_removes_the_session_even_when_the_backend_fails(
    session_manager: SessionManager, respx_mock: respx.Router
) -> None:
    identity = ClientIdentity(value="client-a", source="test")
    await make_session(session_manager, identity)
    respx_mock.post(f"{API_URL}/auth/logout").mock(
        return_value=httpx.Response(500, json=envelope(None, status="failed", response_code=500))
    )

    acknowledged = await session_manager.logout(identity.session_key, device_id="device-1", locale="en")

    assert acknowledged is False
    assert await session_manager.get_session(identity.session_key) is None


async def test_store_returns_copies_so_callers_cannot_mutate_shared_state(
    store: InMemorySessionStore, session_manager: SessionManager
) -> None:
    identity = ClientIdentity(value="client-a", source="test")
    await make_session(session_manager, identity)

    first = await store.get_session(identity.session_key)
    assert first is not None
    first.user_id = "tampered"

    second = await store.get_session(identity.session_key)
    assert second is not None
    assert second.user_id != "tampered"


async def test_challenges_expire(session_manager: SessionManager) -> None:
    from esim_mcp.models.auth import LoginType, OtpChannel
    from esim_mcp.session.models import LoginChallenge

    key = "some-session-key"
    await session_manager.store_challenge(
        key,
        LoginChallenge(
            masked_identifier="p***@example.com",
            login_type=LoginType.EMAIL,
            otp_channel=OtpChannel.EMAIL,
            device_id="device-1",
            created_at=datetime.now(tz=UTC) - timedelta(seconds=600),
            ttl_seconds=300,
        ),
    )

    assert await session_manager.get_challenge(key) is None


async def test_opaque_access_token_is_not_refreshed_proactively(
    session_manager: SessionManager, respx_mock: respx.Router
) -> None:
    """No readable ``exp`` means reactive (401) refresh only -- never a refresh loop."""
    identity = ClientIdentity(value="client-a", source="test")
    await make_session(session_manager, identity, access_token="opaque-token-without-exp")
    route = respx_mock.post(f"{API_URL}/auth/refresh-token")

    session = await session_manager.ensure_fresh_session(
        identity.session_key, device_id="device-1", locale="en", currency="USD"
    )

    assert session.expires_at is None
    assert route.call_count == 0


def test_auth_client_is_never_handed_tokens_by_the_tool_layer(auth_client: AuthApiClient) -> None:
    """Token parameters exist only on the internal client, never on tool signatures."""
    import inspect

    from esim_mcp.tools.authentication import AuthenticationService

    for name, member in inspect.getmembers(AuthenticationService, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        parameters = set(inspect.signature(member).parameters)
        assert not parameters & {"access_token", "refresh_token", "token", "client_id", "device_id"}
