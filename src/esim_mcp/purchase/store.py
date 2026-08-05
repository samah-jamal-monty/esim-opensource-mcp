"""Quote storage abstraction plus the Phase 3 in-memory implementation.

:class:`PurchaseQuoteStore` is the seam that lets the storage backend become an encrypted
Redis store later **without touching the MCP tools**: the tools talk only to
:class:`~esim_mcp.purchase.service.PurchaseQuoteService`, and the service talks only to this
interface. A Redis implementation would back :meth:`PurchaseQuoteStore.lock` with a
distributed lock and let Redis TTLs expire records; nothing above this module would change.

Ownership is enforced *by the store*, not by its callers
--------------------------------------------------------
Every read, cancel and listing takes an :class:`QuoteOwner` and is scoped to it. A quote
belonging to somebody else is therefore not "refused" -- it is simply not found, so this
server cannot be used to discover that another client's quote exists. Owner keys are
compared with :func:`hmac.compare_digest`, so a comparison cannot leak a prefix through
timing.

.. warning::
   :class:`InMemoryPurchaseQuoteStore` keeps quotes in the process heap. **Every prepared
   quote is lost when the MCP server restarts, and quotes are not shared between replicas.**
   That is expected and safe in this phase: a quote holds no money, no reservation and no
   backend order, so losing one costs the user a re-prepare and nothing else. A horizontally
   scaled deployment needs the shared encrypted store described above.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from esim_mcp.purchase.models import PurchaseQuote, QuoteStatus, utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QuoteOwner:
    """The two-part owner of a quote: verified MCP client, plus authenticated eSIM user.

    Both halves are digests, never a raw principal, email or user id.
    """

    session_key: str
    user_ref: str

    def matches(self, quote: PurchaseQuote) -> bool:
        """Constant-time comparison of both halves of the owner identity."""
        return hmac.compare_digest(self.session_key, quote.owner_session_key) and hmac.compare_digest(
            self.user_ref, quote.owner_user_ref
        )

    @property
    def lock_key(self) -> str:
        """Key used to serialize quote creation for one owner."""
        return f"{self.session_key}:{self.user_ref}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"QuoteOwner(session_key={self.session_key[:8]}..., user_ref={self.user_ref[:8]}...)"


class PurchaseQuoteStore(ABC):
    """Storage contract for prepared purchase quotes."""

    @abstractmethod
    async def save(self, quote: PurchaseQuote) -> None:
        """Insert or replace one quote."""

    @abstractmethod
    async def get(self, owner: QuoteOwner, quote_id: str) -> PurchaseQuote | None:
        """Return the quote if it exists **and** belongs to ``owner``, else ``None``."""

    @abstractmethod
    async def list_active(self, owner: QuoteOwner, *, now: datetime | None = None) -> list[PurchaseQuote]:
        """Every quote of ``owner`` that is still active and not yet expired."""

    @abstractmethod
    async def set_status(self, owner: QuoteOwner, quote_id: str, status: QuoteStatus) -> PurchaseQuote | None:
        """Transition one owned quote, returning the updated record, or ``None`` if absent."""

    @abstractmethod
    async def invalidate_session(self, session_key: str) -> int:
        """Cancel every quote held under one MCP session key. Returns how many were cancelled.

        Called on logout and on session invalidation: a quote outliving the session that
        created it would be usable by whoever signs in next on the same client.
        """

    @abstractmethod
    def lock(self, owner: QuoteOwner) -> AbstractAsyncContextManager[None]:
        """Mutual exclusion for one owner, so concurrent preparations cannot interleave."""

    async def aclose(self) -> None:
        """Release any backend resources. No-op by default."""
        return None


class InMemoryPurchaseQuoteStore(PurchaseQuoteStore):
    """Process-local quote store. Local/single-instance operation only.

    Quotes are copied in and out, so a caller can never mutate stored state by holding on to
    a returned model.
    """

    def __init__(self) -> None:
        self._quotes: dict[str, PurchaseQuote] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def save(self, quote: PurchaseQuote) -> None:
        async with self._guard:
            self._quotes[quote.quote_id] = quote.model_copy(deep=True)

    async def get(self, owner: QuoteOwner, quote_id: str) -> PurchaseQuote | None:
        if not quote_id:
            return None
        async with self._guard:
            quote = self._quotes.get(quote_id)
            if quote is None:
                return None
            if not owner.matches(quote):
                # Not an error: an unowned quote is invisible, so nothing here reveals that
                # some other client holds this id.
                logger.info("quote_lookup_owner_mismatch")
                return None
            return quote.model_copy(deep=True)

    async def list_active(self, owner: QuoteOwner, *, now: datetime | None = None) -> list[PurchaseQuote]:
        moment = now or utc_now()
        async with self._guard:
            return [
                quote.model_copy(deep=True)
                for quote in self._quotes.values()
                if owner.matches(quote) and quote.is_usable(moment)
            ]

    async def set_status(self, owner: QuoteOwner, quote_id: str, status: QuoteStatus) -> PurchaseQuote | None:
        async with self._guard:
            quote = self._quotes.get(quote_id)
            if quote is None or not owner.matches(quote):
                return None
            updated = quote.model_copy(update={"status": status})
            self._quotes[quote_id] = updated
            return updated.model_copy(deep=True)

    async def invalidate_session(self, session_key: str) -> int:
        async with self._guard:
            cancelled = 0
            for quote_id, quote in list(self._quotes.items()):
                if not hmac.compare_digest(quote.owner_session_key, session_key):
                    continue
                if quote.status is not QuoteStatus.ACTIVE:
                    continue
                self._quotes[quote_id] = quote.model_copy(update={"status": QuoteStatus.CANCELLED})
                cancelled += 1
            return cancelled

    @asynccontextmanager
    async def lock(self, owner: QuoteOwner) -> AsyncIterator[None]:  # type: ignore[override]
        async with self._guard:
            lock = self._locks.setdefault(owner.lock_key, asyncio.Lock())
        async with lock:
            yield

    async def aclose(self) -> None:
        async with self._guard:
            self._quotes.clear()
            self._locks.clear()
