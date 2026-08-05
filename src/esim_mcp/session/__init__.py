"""Server-side session management: identity, models, storage and lifecycle."""

from esim_mcp.session.identity import (
    AuthenticatedTransportIdentityProvider,
    ClientIdentity,
    ClientIdentityProvider,
    DevelopmentIdentityProvider,
    ResolvingClientIdentityProvider,
    build_identity_provider,
    derive_device_id,
)
from esim_mcp.session.manager import SessionManager
from esim_mcp.session.models import LoginChallenge, UserSession
from esim_mcp.session.store import InMemorySessionStore, SessionStore

__all__ = [
    "AuthenticatedTransportIdentityProvider",
    "ClientIdentity",
    "ClientIdentityProvider",
    "DevelopmentIdentityProvider",
    "InMemorySessionStore",
    "LoginChallenge",
    "ResolvingClientIdentityProvider",
    "SessionManager",
    "SessionStore",
    "UserSession",
    "build_identity_provider",
    "derive_device_id",
]
