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
* **Streamable HTTP without OAuth**: there is no verified *user* principal, but the SDK
  mints a per-connection ``Mcp-Session-Id`` and refuses any request whose id it is not
  already tracking. :class:`TransportSessionIdentityProvider` binds to that, which isolates
  connections from one another.
* **stdio**: one process, one pipe pair, one user, so the process boundary *is* the user
  boundary. :class:`StdioProcessIdentityProvider` covers that case and refuses to answer for
  any other transport.
* **No self-asserted header is ever trusted.** ``X-Client-Id`` and friends are ignored
  outright. ``Mcp-Session-Id`` is the one exception, and only because it is not
  self-asserted: the server generates it and routes on it, so a client cannot present one
  the server did not issue.

Absence of any of the above is fatal for the call
(:class:`~esim_mcp.errors.IdentityUnavailableError`). The server never falls back to a
constant, guessable or shared identity, in any environment.

.. warning::
   **A connection is not an account.** :class:`TransportSessionIdentityProvider` makes two
   concurrent MCP clients two different callers, which is what stops one user inheriting
   another's login. It does *not* recognize a returning user: a new connection is a new
   caller who has to sign in again, and nothing survives a process restart or a move between
   deployment instances. Durable per-user identity needs OAuth
   (:class:`AuthenticatedTransportIdentityProvider`) -- the only provider here that
   identifies a *person* rather than a connection.

History
-------
This module previously fell back to :class:`DevelopmentIdentityProvider` -- a **single
constant principal** -- whenever no OAuth principal existed, in every non-production
environment. Over Streamable HTTP that made every connected user resolve to one session key,
one stored session and one bearer token: user B was logged in as user A. The fallback is
gone, and :func:`build_identity_provider` no longer constructs it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import principal_components

from esim_mcp.errors import IdentityUnavailableError
from esim_mcp.settings import Settings, Transport

logger = logging.getLogger(__name__)

_SESSION_KEY_NAMESPACE = b"esim-mcp/session-key/v1"
_DEVICE_ID_NAMESPACE = "esim-mcp/device-id/v1"

SOURCE_AUTHENTICATED = "authenticated-transport"
SOURCE_TRANSPORT_SESSION = "transport-session"
SOURCE_STDIO_PROCESS = "stdio-process"
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


#: The Streamable HTTP session header, as named by the MCP specification and by the SDK.
MCP_SESSION_ID_HEADER = "mcp-session-id"


class TransportSessionIdentityProvider(ClientIdentityProvider):
    """Binds to the Streamable HTTP session the SDK created for this connection.

    Why this value is trustworthy, despite arriving in a header
    ----------------------------------------------------------
    It is not a self-asserted identity. The SDK **generates** it (``uuid4().hex``, 128 bits)
    when a client initializes, returns it once, and thereafter uses it to route: the session
    manager looks the incoming id up in its table of live transports and answers ``404`` when
    there is no match. A tool therefore only ever runs for an id the *server* minted and is
    still tracking, and there is no string a caller can invent to reach another connection.

    That makes it a bearer credential for the connection -- the same trust model as a session
    cookie -- so it is hashed into :attr:`ClientIdentity.session_key` rather than stored, and
    it is never logged.

    Why not the ``ServerSession`` object
    ------------------------------------
    Measured, not assumed: the SDK builds a **new** ``ServerSession`` per request while the
    session id stays constant for the connection. Keying on the object would mint a fresh
    identity on every call, so a user would be signed out between one tool call and the next
    -- fail-closed, but unusable.

    Fails closed: no request, or a request without the header, raises rather than guessing.
    """

    @staticmethod
    def _headers(ctx: Any) -> Any:
        request = getattr(getattr(ctx, "request_context", None), "request", None)
        headers = getattr(request, "headers", None)
        if headers is not None:
            return headers
        # The SDK also exposes the request headers directly on the tool Context.
        return getattr(ctx, "headers", None)

    async def resolve(self, ctx: Any | None = None) -> ClientIdentity:
        if ctx is None:
            raise IdentityUnavailableError()
        headers = self._headers(ctx)
        if headers is None:
            raise IdentityUnavailableError()
        try:
            session_id = headers.get(MCP_SESSION_ID_HEADER)
        except AttributeError as exc:
            raise IdentityUnavailableError() from exc
        if not session_id or not str(session_id).strip():
            raise IdentityUnavailableError()
        principal = json.dumps(
            {"mcp_session_id": str(session_id).strip()}, separators=(",", ":"), sort_keys=True
        )
        return ClientIdentity(value=principal, source=SOURCE_TRANSPORT_SESSION)


class StdioProcessIdentityProvider(ClientIdentityProvider):
    """The single local user of a **stdio** process.

    Legitimate here and nowhere else: a stdio server talks to exactly one client over one
    pipe pair, so the process boundary *is* the user boundary and a per-process identity
    names precisely one person. The same reasoning is false for Streamable HTTP, where one
    process serves many users -- which is why this provider checks the configured transport
    and refuses otherwise, and why the resolver never reaches it when a request is present.

    The identity is random per process rather than a configured constant, so two processes
    are never the same caller even with identical configuration.
    """

    def __init__(self, settings: Settings) -> None:
        if settings.is_production:
            raise IdentityUnavailableError("The stdio process identity is not available in production.")
        self._settings = settings
        self._token = secrets.token_hex(32)

    async def resolve(self, ctx: Any | None = None) -> ClientIdentity:
        if self._settings.transport is not Transport.STDIO or self._settings.is_production:
            raise IdentityUnavailableError()
        principal = json.dumps({"stdio_process": self._token}, separators=(",", ":"), sort_keys=True)
        return ClientIdentity(value=principal, source=SOURCE_STDIO_PROCESS)


class DevelopmentIdentityProvider(ClientIdentityProvider):
    """Fixed local identity for a single-operator, single-user process.

    .. danger::
       **Never wire this into a multi-client transport.** It returns one constant principal,
       so every caller that reaches it shares one session key, one stored session and one
       bearer token. Doing exactly that over Streamable HTTP is what let one user inherit
       another's login. :func:`build_identity_provider` no longer constructs it, and nothing
       in the shipped resolver falls back to it.

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
    """The verified user principal when there is one; otherwise this connection, alone.

    Two steps, in this order, and no third:

    1. :class:`AuthenticatedTransportIdentityProvider` -- an OAuth-verified principal. This
       is the only identity that names a *person*, so it is the only one under which a
       returning user keeps their session.
    2. :class:`TransportSessionIdentityProvider` -- this MCP connection and no other. It
       cannot recognize a returning user, but it guarantees that two concurrent users are
       two different callers with two different sessions and two different tokens.

    If neither resolves, the call fails closed. There is deliberately no environment in which
    a missing identity degrades to a shared or constant one: that behaviour is what leaked
    one user's authenticated session to another, and removing it is the fix.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        authenticated: ClientIdentityProvider | None = None,
        transport_session: ClientIdentityProvider | None = None,
        stdio_process: ClientIdentityProvider | None = None,
    ) -> None:
        self._settings = settings
        self._authenticated = authenticated or AuthenticatedTransportIdentityProvider()
        self._transport_session = transport_session or TransportSessionIdentityProvider()
        self._stdio_process = stdio_process
        if self._stdio_process is None and settings.transport is Transport.STDIO and not settings.is_production:
            self._stdio_process = StdioProcessIdentityProvider(settings)

    async def resolve(self, ctx: Any | None = None) -> ClientIdentity:
        for provider in self._providers(ctx):
            try:
                identity = await provider.resolve(ctx)
            except IdentityUnavailableError:
                continue
            logger.debug("client_identity_resolved", extra={"identity_source": identity.source})
            return identity

        # Nothing in context ties this call to one caller, so it is not attributable to
        # anybody. Refusing costs a caller one error; guessing costs a user their account.
        logger.warning("client_identity_unavailable_fail_closed")
        raise IdentityUnavailableError()

    def _providers(self, ctx: Any | None) -> list[ClientIdentityProvider]:
        """The chain to try, in order, for this particular call.

        The stdio provider is omitted the moment an HTTP request is in context, whatever the
        configuration says. That is the structural half of the fix: on a multiplexed
        transport there is no code path that can reach a per-process identity, so a
        misconfigured ``ESIM_MCP_TRANSPORT`` cannot reintroduce a shared session.
        """
        chain: list[ClientIdentityProvider] = [self._authenticated, self._transport_session]
        if self._stdio_process is not None and not _has_http_request(ctx):
            chain.append(self._stdio_process)
        return chain


def _has_http_request(ctx: Any | None) -> bool:
    """True when this call arrived over an HTTP transport, i.e. a multiplexed one."""
    if ctx is None:
        return False
    if getattr(getattr(ctx, "request_context", None), "request", None) is not None:
        return True
    return getattr(ctx, "headers", None) is not None


def build_identity_provider(settings: Settings) -> ClientIdentityProvider:
    """Construct the identity provider for this deployment.

    Never returns anything that can produce a shared identity:
    :class:`DevelopmentIdentityProvider` is not constructed here in any environment.
    """
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
