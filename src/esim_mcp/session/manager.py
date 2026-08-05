"""Session lifecycle, token refresh and authenticated-call orchestration.

Token refresh is deliberately *not* an MCP tool: clients never see, send or trigger
tokens directly. Refresh happens here, proactively before expiry and reactively once on
a ``401``, always under a per-session lock so parallel tool calls for the same user
produce exactly one backend rotation.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TypeVar

from pydantic import SecretStr

from esim_mcp.client.auth import AuthApiClient
from esim_mcp.errors import AuthenticationRequiredError, EsimMcpError
from esim_mcp.models.auth import BackendAuthResponse
from esim_mcp.safety.redaction import mask_email, mask_phone
from esim_mcp.session.identity import ClientIdentity
from esim_mcp.session.models import LoginChallenge, UserSession, utc_now
from esim_mcp.session.store import SessionStore
from esim_mcp.settings import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SessionManager:
    """Owns every read and write of server-side session state."""

    def __init__(self, settings: Settings, store: SessionStore, auth_client: AuthApiClient) -> None:
        self._settings = settings
        self._store = store
        self._auth_client = auth_client
        self._invalidation_listeners: list[Callable[[str], Awaitable[None]]] = []

    # ------------------------------------------------------- invalidation listeners

    def add_invalidation_listener(self, listener: Callable[[str], Awaitable[None]]) -> None:
        """Register state that must be dropped whenever a session goes away.

        Phase 3 uses this so prepared purchase quotes cannot outlive the session that created
        them: a quote surviving a logout would be readable by whoever signs in next on the
        same MCP client. Registration rather than a constructor argument keeps the session
        layer free of any dependency on the purchase layer -- this class never learns what a
        quote is.
        """
        self._invalidation_listeners.append(listener)

    async def _notify_invalidated(self, session_key: str) -> None:
        """Run every listener. A failing listener may not abort a logout.

        Losing a session is the security-relevant half; a listener that cannot clean up its
        own state is logged and skipped rather than allowed to leave the session in place.
        """
        for listener in self._invalidation_listeners:
            try:
                await listener(session_key)
            except Exception:
                logger.exception(
                    "session_invalidation_listener_failed", extra={"session_ref": session_key_ref(session_key)}
                )

    # ------------------------------------------------------------------ challenges

    async def store_challenge(self, session_key: str, challenge: LoginChallenge) -> None:
        """Persist a pending login challenge. Contains no OTP and no raw identifier."""
        await self._store.save_challenge(session_key, challenge)

    async def get_challenge(self, session_key: str, *, now: datetime | None = None) -> LoginChallenge | None:
        """Return the pending challenge, dropping it when it has expired."""
        challenge = await self._store.get_challenge(session_key)
        if challenge is None:
            return None
        if challenge.is_expired(now):
            await self._store.delete_challenge(session_key)
            return None
        return challenge

    async def clear_challenge(self, session_key: str) -> None:
        await self._store.delete_challenge(session_key)

    # -------------------------------------------------------------------- sessions

    async def get_session(self, session_key: str) -> UserSession | None:
        return await self._store.get_session(session_key)

    async def require_session(self, session_key: str) -> UserSession:
        session = await self._store.get_session(session_key)
        if session is None:
            raise AuthenticationRequiredError()
        return session

    async def create_session(
        self,
        identity: ClientIdentity,
        *,
        device_id: str,
        auth: BackendAuthResponse,
        currency: str,
        fallback_email: str | None = None,
        fallback_phone: str | None = None,
    ) -> UserSession:
        """Store tokens for a freshly verified user under their own identity key."""
        session = UserSession(
            session_key=identity.session_key,
            identity_source=identity.source,
            device_id=device_id,
            access_token=auth.access_token,
            refresh_token=auth.refresh_token,
            expires_at=auth.access_token_expiry(),
            user_id=auth.user_token,
            masked_email=mask_email(auth.user_info.email) or mask_email(fallback_email),
            masked_phone=mask_phone(auth.user_info.msisdn) or mask_phone(fallback_phone),
            currency=auth.user_info.currency_code or currency,
            is_verified=auth.is_verified or auth.user_info.is_verified,
        )
        await self._store.save_session(session)
        await self._store.delete_challenge(identity.session_key)
        logger.info(
            "session_created",
            extra={"session_ref": session_key_ref(session.session_key), "identity_source": identity.source},
        )
        return session

    async def invalidate(self, session_key: str) -> None:
        """Drop the local session for one client only, and everything hanging off it."""
        await self._store.delete_session(session_key)
        await self._store.delete_challenge(session_key)
        await self._notify_invalidated(session_key)
        logger.info("session_invalidated", extra={"session_ref": session_key_ref(session_key)})

    # --------------------------------------------------------------------- refresh

    async def refresh(
        self,
        session_key: str,
        *,
        device_id: str,
        locale: str,
        currency: str,
        stale_token: SecretStr | None = None,
        force: bool = False,
    ) -> UserSession:
        """Rotate tokens under the per-session lock.

        Returns early when another task already rotated (double-checked inside the lock),
        so concurrent callers produce a single backend request. A failed rotation
        invalidates the local session and raises ``AuthenticationRequiredError``; it is
        never retried.
        """
        async with self._store.lock(session_key):
            session = await self._store.get_session(session_key)
            if session is None:
                raise AuthenticationRequiredError()

            if stale_token is not None and session.access_token.get_secret_value() != stale_token.get_secret_value():
                return session
            if not force and not session.needs_refresh(self._settings.token_refresh_window_seconds):
                return session

            try:
                auth = await self._auth_client.refresh_token(
                    device_id=device_id,
                    refresh_token=session.refresh_token,
                    locale=locale,
                    currency=currency,
                )
            except EsimMcpError:
                await self._store.delete_session(session_key)
                # A session that could not be rotated is gone for good, so anything scoped to
                # it (a prepared quote) has to go with it.
                await self._notify_invalidated(session_key)
                logger.warning("token_refresh_failed", extra={"session_ref": session_key_ref(session_key)})
                raise AuthenticationRequiredError(
                    "The eSIM session could not be refreshed. Sign in again with a new verification code."
                ) from None

            rotated = session.model_copy(
                update={
                    "access_token": auth.access_token,
                    "refresh_token": auth.refresh_token,
                    "expires_at": auth.access_token_expiry(),
                    "user_id": auth.user_token or session.user_id,
                    "masked_email": mask_email(auth.user_info.email) or session.masked_email,
                    "masked_phone": mask_phone(auth.user_info.msisdn) or session.masked_phone,
                    "currency": auth.user_info.currency_code or session.currency,
                    "updated_at": utc_now(),
                }
            )
            await self._store.save_session(rotated)
            logger.info("token_refreshed", extra={"session_ref": session_key_ref(session_key)})
            return rotated

    async def ensure_fresh_session(
        self,
        session_key: str,
        *,
        device_id: str,
        locale: str,
        currency: str,
    ) -> UserSession:
        """Return a session whose access token is outside the refresh safety window."""
        session = await self.require_session(session_key)
        if session.needs_refresh(self._settings.token_refresh_window_seconds):
            session = await self.refresh(session_key, device_id=device_id, locale=locale, currency=currency)
        return session

    async def run_authenticated(
        self,
        session_key: str,
        operation: Callable[[SecretStr], Awaitable[T]],
        *,
        device_id: str,
        locale: str,
        currency: str,
        allow_refresh_replay: bool = True,
    ) -> T:
        """Run an authenticated backend call, refreshing at most once.

        The single replay is allowed only for operations the caller marks as safe to
        repeat; there is never a second refresh and never a retry loop.
        """
        session = await self.ensure_fresh_session(session_key, device_id=device_id, locale=locale, currency=currency)
        try:
            return await operation(session.access_token)
        except AuthenticationRequiredError:
            if not allow_refresh_replay:
                await self.invalidate(session_key)
                raise

        refreshed = await self.refresh(
            session_key,
            device_id=device_id,
            locale=locale,
            currency=currency,
            stale_token=session.access_token,
            force=True,
        )
        try:
            return await operation(refreshed.access_token)
        except AuthenticationRequiredError:
            await self.invalidate(session_key)
            raise AuthenticationRequiredError() from None

    # ---------------------------------------------------------------------- logout

    async def logout(self, session_key: str, *, device_id: str, locale: str) -> bool:
        """Best-effort backend logout, then unconditional local removal.

        Only the calling client's session is touched. Returns whether the backend
        acknowledged the logout.
        """
        session = await self._store.get_session(session_key)
        if session is None:
            raise AuthenticationRequiredError()
        backend_acknowledged = True
        try:
            await self._auth_client.logout(device_id=device_id, access_token=session.access_token, locale=locale)
        except EsimMcpError:
            backend_acknowledged = False
            logger.info("backend_logout_unconfirmed", extra={"session_ref": session_key_ref(session_key)})
        finally:
            await self.invalidate(session_key)
        return backend_acknowledged


def session_key_ref(session_key: str) -> str:
    """Short, non-reversible reference safe to put in a log record."""
    return session_key[:8]
