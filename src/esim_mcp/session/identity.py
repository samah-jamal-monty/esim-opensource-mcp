"""MCP client identity resolution and stable device-id derivation.

Identity is the key everything else hangs off: it selects the session, and it seeds the
``X-Device-Id`` the backend sees. It therefore may never come from a normal tool
argument or from a self-asserted header.

What the installed SDK (``mcp`` 2.x) actually offers
----------------------------------------------------
* **Streamable HTTP with OAuth configured** (``MCPServer(auth=..., token_verifier=...)``):
  the SDK verifies the bearer token and exposes the resulting principal through
  ``mcp.server.auth.middleware.auth_context.get_access_token()``. The
  ``(client_id, issuer, subject)`` triple returned by ``principal_components`` is the
  same identity the SDK itself uses for session ownership, and it is what
  :class:`AuthenticatedTransportIdentityProvider` binds to.
* **stdio**: the transport has no principal at all -- the process *is* the trust
  boundary. :class:`DevelopmentIdentityProvider` supplies a configured, stable local
  identity there.
* **Streamable HTTP without OAuth**: there is no verified principal. The SDK explicitly
  documents request headers as "client-supplied input - never treat one as an identity
  assertion", so no header (including ``X-Client-Id``) is trusted here.

In production, absence of a verified principal is fatal for the call
(:class:`~esim_mcp.errors.IdentityUnavailableError`); the server never falls back to a
guessable identity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import principal_components

from esim_mcp.errors import IdentityUnavailableError
from esim_mcp.settings import Settings

logger = logging.getLogger(__name__)

_SESSION_KEY_NAMESPACE = b"esim-mcp/session-key/v1"
_DEVICE_ID_NAMESPACE = "esim-mcp/device-id/v1"

SOURCE_AUTHENTICATED = "authenticated-transport"
SOURCE_DEVELOPMENT = "development-stdio"


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """A resolved MCP caller.

    ``value`` is the verified principal string. It is opaque, never returned to clients
    and never logged; only :attr:`session_key` (a digest) is safe to record.
    """

    value: str
    source: str

    @property
    def session_key(self) -> str:
        """Stable, non-reversible key used for session storage."""
        digest = hashlib.sha256()
        digest.update(_SESSION_KEY_NAMESPACE)
        digest.update(self.value.encode("utf-8"))
        return digest.hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ClientIdentity(source={self.source!r}, session_key={self.session_key[:8]}...)"


class ClientIdentityProvider(ABC):
    """Strategy for turning a request context into a verified :class:`ClientIdentity`."""

    @abstractmethod
    async def resolve(self, ctx: Any | None = None) -> ClientIdentity:
        """Return the caller's identity or raise :class:`IdentityUnavailableError`."""


class AuthenticatedTransportIdentityProvider(ClientIdentityProvider):
    """Binds to the principal verified by the MCP transport's OAuth token verifier."""

    async def resolve(self, ctx: Any | None = None) -> ClientIdentity:
        token = get_access_token()
        if token is None:
            raise IdentityUnavailableError()
        client_id, issuer, subject = principal_components(token)
        principal = json.dumps(
            {"client_id": client_id, "issuer": issuer, "subject": subject},
            separators=(",", ":"),
            sort_keys=True,
        )
        return ClientIdentity(value=principal, source=SOURCE_AUTHENTICATED)


class DevelopmentIdentityProvider(ClientIdentityProvider):
    """Fixed local identity for stdio / single-operator development.

    Refuses to be constructed for a production configuration, so a misconfiguration
    cannot quietly downgrade identity verification.
    """

    def __init__(self, settings: Settings) -> None:
        if settings.is_production:
            raise IdentityUnavailableError("The development identity provider is not available in production.")
        self._client_id = settings.dev_client_id

    async def resolve(self, ctx: Any | None = None) -> ClientIdentity:
        principal = json.dumps({"dev_client_id": self._client_id}, separators=(",", ":"), sort_keys=True)
        return ClientIdentity(value=principal, source=SOURCE_DEVELOPMENT)


class ResolvingClientIdentityProvider(ClientIdentityProvider):
    """Prefers the authenticated transport principal; falls back only outside production."""

    def __init__(
        self,
        settings: Settings,
        *,
        authenticated: ClientIdentityProvider | None = None,
        development: ClientIdentityProvider | None = None,
    ) -> None:
        self._settings = settings
        self._authenticated = authenticated or AuthenticatedTransportIdentityProvider()
        self._development = development
        if self._development is None and settings.allows_development_identity:
            self._development = DevelopmentIdentityProvider(settings)

    async def resolve(self, ctx: Any | None = None) -> ClientIdentity:
        try:
            return await self._authenticated.resolve(ctx)
        except IdentityUnavailableError:
            if self._development is None or not self._settings.allows_development_identity:
                logger.warning("client_identity_unavailable_fail_closed")
                raise
        identity = await self._development.resolve(ctx)
        logger.debug("client_identity_resolved", extra={"identity_source": identity.source})
        return identity


def build_identity_provider(settings: Settings) -> ClientIdentityProvider:
    """Construct the identity provider appropriate for the configured environment."""
    return ResolvingClientIdentityProvider(settings)


def derive_device_id(salt: bytes, identity: ClientIdentity | str) -> str:
    """``HMAC-SHA256(salt, verified_client_identity)`` as lowercase hex.

    Stable for a given (salt, identity) pair across logins and restarts, different for
    different clients, and non-reversible: the raw identity is not recoverable from it.
    Python's built-in ``hash()`` is never used -- it is randomized per process.
    """
    if not salt:
        raise IdentityUnavailableError("No device-id salt is configured.")
    value = identity.value if isinstance(identity, ClientIdentity) else identity
    message = f"{_DEVICE_ID_NAMESPACE}|{value}".encode()
    return hmac.new(salt, message, hashlib.sha256).hexdigest()
