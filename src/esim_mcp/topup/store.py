"""Storage abstractions for top-up quotes and wallet-top-up checkouts.

The same seam :mod:`esim_mcp.purchase.store` establishes, for the same reason: the tools
talk only to the services in :mod:`esim_mcp.topup.service`, and those talk only to these
interfaces, so an encrypted Redis implementation would slot in without any tool changing.

Ownership is enforced *by the store*, not by its callers
--------------------------------------------------------
Every read and every transition takes a :class:`~esim_mcp.purchase.store.QuoteOwner` and is
scoped to it. A record belonging to somebody else is therefore not "refused" -- it is simply
not found, so this server cannot be used to discover that another client's top-up exists.
Owner keys are compared with :func:`hmac.compare_digest`, so a comparison cannot leak a
prefix through timing.

.. warning::
   The in-memory implementations keep records in the process heap, so they are lost on a
   restart and not shared between replicas. For a **quote** that costs a user a
   re-preparation and nothing else: a quote holds no money and no platform state. For a
   wallet-top-up **checkout** it costs a repeated call to the platform, which answers with
   the page it already opened -- the duplicate protection is the platform's pending order
   row, and that is unaffected by anything here.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime

from esim_mcp.purchase.store import QuoteOwner
from esim_mcp.topup.models import (
    EsimTopupExecution,
    EsimTopupQuote,
    TopupQuoteStatus,
    WalletTopupCheckout,
    WalletTopupQuote,
    utc_now,
)

logger = logging.getLogger(__name__)


def owner_matches(owner: QuoteOwner, record: object) -> bool:
    """Constant-time comparison of both halves of the owner identity."""
    session_key = getattr(record, "owner_session_key", "")
    user_ref = getattr(record, "owner_user_ref", "")
    return hmac.compare_digest(owner.session_key, session_key) and hmac.compare_digest(owner.user_ref, user_ref)


# ------------------------------------------------------------------------ eSIM top-up quotes


class EsimTopupQuoteStore(ABC):
    """Storage contract for prepared eSIM top-up quotes."""

    @abstractmethod
    async def save(self, quote: EsimTopupQuote) -> None:
        """Insert or replace one quote."""

    @abstractmethod
    async def get(self, owner: QuoteOwner, quote_id: str) -> EsimTopupQuote | None:
        """Return the quote if it exists **and** belongs to ``owner``, else ``None``."""

    @abstractmethod
    async def list_active(self, owner: QuoteOwner, *, now: datetime | None = None) -> list[EsimTopupQuote]:
        """Every quote of ``owner`` that is still active and not yet expired."""

    @abstractmethod
    async def set_status(
        self, owner: QuoteOwner, quote_id: str, status: TopupQuoteStatus
    ) -> EsimTopupQuote | None:
        """Transition one owned quote, returning the updated record, or ``None`` if absent."""

    @abstractmethod
    async def invalidate_session(self, session_key: str) -> int:
        """Cancel every quote held under one MCP session key. Returns how many were cancelled."""

    @abstractmethod
    def lock(self, owner: QuoteOwner) -> AbstractAsyncContextManager[None]:
        """Mutual exclusion for one owner, so concurrent preparations cannot interleave."""

    async def aclose(self) -> None:
        """Release any backend resources. No-op by default."""
        return None


class InMemoryEsimTopupQuoteStore(EsimTopupQuoteStore):
    """Process-local eSIM top-up quote store. Local/single-instance operation only."""

    def __init__(self) -> None:
        self._quotes: dict[str, EsimTopupQuote] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def save(self, quote: EsimTopupQuote) -> None:
        async with self._guard:
            self._quotes[quote.quote_id] = quote.model_copy(deep=True)

    async def get(self, owner: QuoteOwner, quote_id: str) -> EsimTopupQuote | None:
        if not quote_id:
            return None
        async with self._guard:
            quote = self._quotes.get(quote_id)
            if quote is None:
                return None
            if not owner_matches(owner, quote):
                # Not an error: an unowned quote is invisible, so nothing here reveals that
                # some other client holds this id -- or tops up this SIM.
                logger.info("esim_topup_quote_lookup_owner_mismatch")
                return None
            return quote.model_copy(deep=True)

    async def list_active(self, owner: QuoteOwner, *, now: datetime | None = None) -> list[EsimTopupQuote]:
        moment = now or utc_now()
        async with self._guard:
            return [
                quote.model_copy(deep=True)
                for quote in self._quotes.values()
                if owner_matches(owner, quote) and quote.effective_status(moment) is TopupQuoteStatus.ACTIVE
            ]

    async def set_status(
        self, owner: QuoteOwner, quote_id: str, status: TopupQuoteStatus
    ) -> EsimTopupQuote | None:
        async with self._guard:
            quote = self._quotes.get(quote_id)
            if quote is None or not owner_matches(owner, quote):
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
                if quote.status is not TopupQuoteStatus.ACTIVE:
                    continue
                self._quotes[quote_id] = quote.model_copy(update={"status": TopupQuoteStatus.CANCELLED})
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


# ------------------------------------------------------------------- eSIM top-up executions


class EsimTopupExecutionStore(ABC):
    """Storage contract for eSIM top-up executions, scoped to one owner exactly like quotes.

    .. danger::
       The in-memory implementation below is **the whole of the duplicate protection** for a
       QA eSIM top-up, and it is process-local. If this server restarts between sending a
       top-up and recording its outcome, the lock is lost and the same quote could be
       confirmed again -- and the platform, which has no idempotency key, would run it a
       second time and charge the user twice.

       This is precisely the property the production build does not rely on: the capability
       is behind a QA-only flag that production settings refuse to construct. Do not promote
       this store to a production guarantee; the fix is durable idempotency at the platform.
    """

    @abstractmethod
    async def get(self, owner: QuoteOwner, quote_id: str) -> EsimTopupExecution | None:
        """Return the execution if it exists **and** belongs to ``owner``, else ``None``."""

    @abstractmethod
    async def save(self, execution: EsimTopupExecution) -> None:
        """Insert or replace one execution record."""

    @abstractmethod
    async def invalidate_session(self, session_key: str) -> int:
        """Drop every execution held under one MCP session key."""

    @abstractmethod
    def lock(self, quote_id: str) -> AbstractAsyncContextManager[None]:
        """Mutual exclusion for one quote, held across the whole call.

        Deliberately held across the backend I/O: two concurrent confirmations of the same
        quote must not both reach the platform, whatever order the client sends them in.
        For a non-idempotent route that race is the difference between one charge and two.
        """

    async def aclose(self) -> None:
        return None


class InMemoryEsimTopupExecutionStore(EsimTopupExecutionStore):
    """Process-local execution store. QA/single-instance operation only."""

    def __init__(self) -> None:
        self._executions: dict[str, EsimTopupExecution] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, owner: QuoteOwner, quote_id: str) -> EsimTopupExecution | None:
        if not quote_id:
            return None
        async with self._guard:
            execution = self._executions.get(quote_id)
            if execution is None:
                return None
            if not owner_matches(owner, execution):
                logger.info("esim_topup_execution_lookup_owner_mismatch")
                return None
            return execution.model_copy(deep=True)

    async def save(self, execution: EsimTopupExecution) -> None:
        async with self._guard:
            self._executions[execution.quote_id] = execution.model_copy(deep=True)

    async def invalidate_session(self, session_key: str) -> int:
        async with self._guard:
            doomed = [
                quote_id
                for quote_id, execution in self._executions.items()
                if hmac.compare_digest(execution.owner_session_key, session_key)
            ]
            for quote_id in doomed:
                del self._executions[quote_id]
            return len(doomed)

    @asynccontextmanager
    async def lock(self, quote_id: str) -> AsyncIterator[None]:  # type: ignore[override]
        async with self._guard:
            lock = self._locks.setdefault(quote_id, asyncio.Lock())
        async with lock:
            yield

    async def aclose(self) -> None:
        async with self._guard:
            self._executions.clear()
            self._locks.clear()


# ---------------------------------------------------------------------- wallet top-up quotes


class WalletTopupQuoteStore(ABC):
    """Storage contract for prepared wallet top-up quotes."""

    @abstractmethod
    async def save(self, quote: WalletTopupQuote) -> None: ...

    @abstractmethod
    async def get(self, owner: QuoteOwner, quote_id: str) -> WalletTopupQuote | None: ...

    @abstractmethod
    async def list_active(self, owner: QuoteOwner, *, now: datetime | None = None) -> list[WalletTopupQuote]: ...

    @abstractmethod
    async def set_status(
        self, owner: QuoteOwner, quote_id: str, status: TopupQuoteStatus
    ) -> WalletTopupQuote | None: ...

    @abstractmethod
    async def invalidate_session(self, session_key: str) -> int: ...

    @abstractmethod
    def lock(self, owner: QuoteOwner) -> AbstractAsyncContextManager[None]: ...

    async def aclose(self) -> None:
        return None


class InMemoryWalletTopupQuoteStore(WalletTopupQuoteStore):
    """Process-local wallet top-up quote store. Local/single-instance operation only."""

    def __init__(self) -> None:
        self._quotes: dict[str, WalletTopupQuote] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def save(self, quote: WalletTopupQuote) -> None:
        async with self._guard:
            self._quotes[quote.quote_id] = quote.model_copy(deep=True)

    async def get(self, owner: QuoteOwner, quote_id: str) -> WalletTopupQuote | None:
        if not quote_id:
            return None
        async with self._guard:
            quote = self._quotes.get(quote_id)
            if quote is None:
                return None
            if not owner_matches(owner, quote):
                logger.info("wallet_topup_quote_lookup_owner_mismatch")
                return None
            return quote.model_copy(deep=True)

    async def list_active(self, owner: QuoteOwner, *, now: datetime | None = None) -> list[WalletTopupQuote]:
        moment = now or utc_now()
        async with self._guard:
            return [
                quote.model_copy(deep=True)
                for quote in self._quotes.values()
                if owner_matches(owner, quote) and quote.effective_status(moment) is TopupQuoteStatus.ACTIVE
            ]

    async def set_status(
        self, owner: QuoteOwner, quote_id: str, status: TopupQuoteStatus
    ) -> WalletTopupQuote | None:
        async with self._guard:
            quote = self._quotes.get(quote_id)
            if quote is None or not owner_matches(owner, quote):
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
                if quote.status is not TopupQuoteStatus.ACTIVE:
                    continue
                self._quotes[quote_id] = quote.model_copy(update={"status": TopupQuoteStatus.CANCELLED})
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


# ------------------------------------------------------------------- wallet top-up checkouts


class WalletTopupCheckoutStore(ABC):
    """Storage contract for wallet-top-up checkouts, scoped to one owner exactly like quotes."""

    @abstractmethod
    async def get(self, owner: QuoteOwner, quote_id: str) -> WalletTopupCheckout | None: ...

    @abstractmethod
    async def get_by_reference(self, owner: QuoteOwner, payment_reference: str) -> WalletTopupCheckout | None:
        """Return the checkout for one payment reference, scoped to ``owner``.

        A reference belonging to another client is *not found* rather than refused, so this
        server cannot be used to discover that somebody else's top-up exists.
        """

    @abstractmethod
    async def save(self, checkout: WalletTopupCheckout) -> None: ...

    @abstractmethod
    async def invalidate_session(self, session_key: str) -> int: ...

    @abstractmethod
    def lock(self, key: str) -> AbstractAsyncContextManager[None]:
        """Mutual exclusion for one quote or one payment, held across the whole call."""

    async def aclose(self) -> None:
        return None


class InMemoryWalletTopupCheckoutStore(WalletTopupCheckoutStore):
    """Process-local checkout store. Local/single-instance operation only."""

    def __init__(self) -> None:
        self._checkouts: dict[str, WalletTopupCheckout] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, owner: QuoteOwner, quote_id: str) -> WalletTopupCheckout | None:
        if not quote_id:
            return None
        async with self._guard:
            checkout = self._checkouts.get(quote_id)
            if checkout is None:
                return None
            if not owner_matches(owner, checkout):
                logger.info("wallet_topup_checkout_lookup_owner_mismatch")
                return None
            return checkout.model_copy(deep=True)

    async def get_by_reference(self, owner: QuoteOwner, payment_reference: str) -> WalletTopupCheckout | None:
        if not payment_reference:
            return None
        async with self._guard:
            for checkout in self._checkouts.values():
                if checkout.payment_reference is None:
                    continue
                if not hmac.compare_digest(checkout.payment_reference, payment_reference):
                    continue
                if not owner_matches(owner, checkout):
                    logger.info("wallet_topup_payment_lookup_owner_mismatch")
                    return None
                return checkout.model_copy(deep=True)
            return None

    async def save(self, checkout: WalletTopupCheckout) -> None:
        async with self._guard:
            self._checkouts[checkout.quote_id] = checkout.model_copy(deep=True)

    async def invalidate_session(self, session_key: str) -> int:
        async with self._guard:
            doomed = [
                quote_id
                for quote_id, checkout in self._checkouts.items()
                if hmac.compare_digest(checkout.owner_session_key, session_key)
            ]
            for quote_id in doomed:
                del self._checkouts[quote_id]
            return len(doomed)

    @asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:  # type: ignore[override]
        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    async def aclose(self) -> None:
        async with self._guard:
            self._checkouts.clear()
            self._locks.clear()
