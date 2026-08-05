"""Session storage abstraction plus the Phase 1 in-memory implementation.

The :class:`SessionStore` interface is the seam that lets the storage backend become an
encrypted Redis store later without touching the tools, the session manager or the API
client. Every operation is asynchronous, and every mutation is guarded so concurrent
tool calls cannot interleave destructively.

.. warning::
   :class:`InMemorySessionStore` keeps state in the process heap. It is correct for
   stdio and for a *single* HTTP instance only. A horizontally scaled deployment needs a
   shared, encrypted store (Redis with encryption at rest and per-session TTLs);
   otherwise a client routed to another replica silently loses its session.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from esim_mcp.session.models import LoginChallenge, UserSession


class SessionStore(ABC):
    """Storage contract for authenticated sessions and pending login challenges."""

    @abstractmethod
    async def get_session(self, session_key: str) -> UserSession | None: ...

    @abstractmethod
    async def save_session(self, session: UserSession) -> None: ...

    @abstractmethod
    async def delete_session(self, session_key: str) -> None: ...

    @abstractmethod
    async def get_challenge(self, session_key: str) -> LoginChallenge | None: ...

    @abstractmethod
    async def save_challenge(self, session_key: str, challenge: LoginChallenge) -> None: ...

    @abstractmethod
    async def delete_challenge(self, session_key: str) -> None: ...

    @abstractmethod
    def lock(self, session_key: str) -> AbstractAsyncContextManager[None]:
        """Mutual exclusion for one session key.

        Used to serialize token refresh. A Redis implementation would back this with a
        distributed lock; the in-memory implementation uses an ``asyncio.Lock``.
        """

    async def aclose(self) -> None:
        """Release any backend resources. No-op by default."""
        return None


class InMemorySessionStore(SessionStore):
    """Process-local store. Local/single-instance operation only."""

    def __init__(self) -> None:
        self._sessions: dict[str, UserSession] = {}
        self._challenges: dict[str, LoginChallenge] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get_session(self, session_key: str) -> UserSession | None:
        async with self._guard:
            session = self._sessions.get(session_key)
            return session.model_copy(deep=True) if session else None

    async def save_session(self, session: UserSession) -> None:
        async with self._guard:
            self._sessions[session.session_key] = session.model_copy(deep=True)

    async def delete_session(self, session_key: str) -> None:
        async with self._guard:
            self._sessions.pop(session_key, None)

    async def get_challenge(self, session_key: str) -> LoginChallenge | None:
        async with self._guard:
            challenge = self._challenges.get(session_key)
            return challenge.model_copy(deep=True) if challenge else None

    async def save_challenge(self, session_key: str, challenge: LoginChallenge) -> None:
        async with self._guard:
            self._challenges[session_key] = challenge.model_copy(deep=True)

    async def delete_challenge(self, session_key: str) -> None:
        async with self._guard:
            self._challenges.pop(session_key, None)

    @asynccontextmanager
    async def lock(self, session_key: str) -> AsyncIterator[None]:  # type: ignore[override]
        async with self._guard:
            lock = self._locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            yield

    async def aclose(self) -> None:
        async with self._guard:
            self._sessions.clear()
            self._challenges.clear()
            self._locks.clear()
