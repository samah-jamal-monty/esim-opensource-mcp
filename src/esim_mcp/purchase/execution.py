"""Purchase-execution bookkeeping: one idempotency key per quote, and one terminal answer.

This module is what makes ``confirm_purchase`` safe to call twice. It performs **no I/O** --
like the rest of :mod:`esim_mcp.purchase` it cannot reach the network, so nothing here can
create an order or move money. What it owns is the decision of *whether a purchase may be
sent at all*, and what to say when one already was.

The rules it enforces
---------------------
* **One key per quote, minted once.** :meth:`PurchaseExecutionService.acquire` mints a
  cryptographically random ``Idempotency-Key`` the first time a quote is confirmed and
  returns that same key for every later attempt on that quote. A transport retry, a
  reconnect, or a model that calls the tool twice therefore all present the platform with
  the *same* key, which is what lets the backend recognize a replay instead of buying the
  plan a second time. A new key is only ever produced by a new quote.
* **A terminal outcome is replayed, never re-executed.** Once an execution has succeeded or
  failed for good, the stored answer is handed back verbatim and no request is sent.
* **An unresolved outcome is never "resolved" locally.** A timeout or an ambiguous backend
  answer is recorded as :attr:`ExecutionStatus.UNRESOLVED`; the *same* key may be presented
  again so the platform can tell us what really happened, and a bounded attempt count
  (:data:`MAX_EXECUTION_ATTEMPTS`) stops that becoming an unattended loop.

.. warning::
   :class:`InMemoryPurchaseExecutionStore` keeps records in the process heap. If this server
   restarts between sending a purchase and recording its outcome, the *local* memory of the
   idempotency key is lost -- the backend's record is not, so the purchase is still protected
   from duplication there, but this server can no longer replay the answer. A durable
   (encrypted Redis) store is the same seam as for quotes; nothing above this module changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from esim_mcp.errors import (
    EsimMcpError,
    IdempotencyConflictError,
    InsufficientWalletBalanceError,
    NonWalletQuoteError,
    PurchaseAttemptLimitError,
    PurchaseCurrencyMismatchError,
    PurchaseInProgressError,
    PurchaseManualInterventionError,
    PurchaseOutcomeUnknownError,
    PurchaseRejectedError,
    PurchaseRouteUnavailableError,
    PurchaseUnavailableError,
)
from esim_mcp.purchase.models import PaymentMethod, PurchaseQuote, utc_now
from esim_mcp.purchase.store import QuoteOwner

logger = logging.getLogger(__name__)

#: Bytes of entropy behind an idempotency key. 48 bytes is 64 url-safe characters, which sits
#: inside the platform's accepted 32..128 length and uses only its accepted alphabet. The
#: value is random, never derived from the quote, the user or the plan.
_IDEMPOTENCY_KEY_BYTES = 48

#: How many times one quote's purchase may be sent before this server refuses to try again.
#: Every attempt after the first carries the *same* key, so this is a guard against an
#: unattended retry loop, not a duplicate-purchase risk.
MAX_EXECUTION_ATTEMPTS = 3

#: Length of the non-reversible fingerprint that may appear in a log record. The raw key
#: never appears in a log, a result, an error or a stored quote.
_FINGERPRINT_LENGTH = 12


def new_idempotency_key() -> str:
    """A cryptographically random idempotency key.

    :mod:`secrets`, never a counter, a timestamp, a hash of the quote or a UUID whose version
    encodes predictable input: two purchases must never be able to collide on a key, and a
    key must not be guessable from anything the caller can see.
    """
    return secrets.token_urlsafe(_IDEMPOTENCY_KEY_BYTES)


def key_fingerprint(key: SecretStr | str) -> str:
    """Short, non-reversible fingerprint of an idempotency key, safe for a log record."""
    raw = key.get_secret_value() if isinstance(key, SecretStr) else key
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_FINGERPRINT_LENGTH]


class ExecutionStatus(StrEnum):
    """Lifecycle of one purchase execution.

    The distinction that matters most is between the two unhappy middles:

    * ``UNRESOLVED`` -- *this server* never learned the outcome (a timeout, a dropped
      connection, a purchase the platform says is still running). The same key may be
      presented again, which is how the platform is asked what really happened.
    * ``ESCALATED`` -- *the platform itself* said it cannot resolve the purchase and that the
      wallet may already have been charged. Sending anything again is exactly the wrong move,
      so this state is replayed forever and never re-executed.

    Only ``SUCCEEDED`` means money moved *and* the platform confirmed it.
    """

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNRESOLVED = "unresolved"
    ESCALATED = "escalated"

    @property
    def is_terminal(self) -> bool:
        """True when the stored answer must be replayed rather than sent again."""
        return self in (ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.ESCALATED)


class PurchaseExecution(BaseModel):
    """One quote's purchase attempt history, including the key it is bound to.

    ``model_config`` is ``extra="forbid"``: no price, balance, token, card detail or backend
    user id can be added to this record by an unvalidated payload. The idempotency key is the
    only secret it holds, and it is a :class:`~pydantic.SecretStr` so it cannot be logged or
    serialized by accident.
    """

    model_config = ConfigDict(extra="forbid")

    quote_id: str
    owner_session_key: str
    owner_user_ref: str
    idempotency_key: SecretStr
    attempts: int = 0
    status: ExecutionStatus = ExecutionStatus.PENDING
    #: The safe result payload replayed to the caller after a success.
    result: dict[str, Any] | None = None
    #: The typed error a terminal failure or an unresolved outcome replays.
    failure_code: str | None = None
    failure_message: str | None = None
    failure_details: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def attempts_remaining(self) -> int:
        return max(0, MAX_EXECUTION_ATTEMPTS - self.attempts)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PurchaseExecution(quote={self.quote_id[:8]}..., status={self.status.value}, attempts={self.attempts})"


class PurchaseExecutionStore(ABC):
    """Storage contract for purchase executions, scoped to one owner exactly like quotes."""

    @abstractmethod
    async def get(self, owner: QuoteOwner, quote_id: str) -> PurchaseExecution | None:
        """Return the execution if it exists **and** belongs to ``owner``, else ``None``."""

    @abstractmethod
    async def save(self, execution: PurchaseExecution) -> None:
        """Insert or replace one execution record."""

    @abstractmethod
    async def invalidate_session(self, session_key: str) -> int:
        """Drop every execution held under one MCP session key. Returns how many were dropped."""

    @abstractmethod
    def lock(self, quote_id: str) -> AbstractAsyncContextManager[None]:
        """Mutual exclusion for one quote, held across the whole purchase call.

        Two concurrent confirmations of the same quote must not both reach the platform, so
        this lock is deliberately held across backend I/O rather than only around the store.
        """

    async def aclose(self) -> None:
        """Release any backend resources. No-op by default."""
        return None


class InMemoryPurchaseExecutionStore(PurchaseExecutionStore):
    """Process-local execution store. Local/single-instance operation only."""

    def __init__(self) -> None:
        self._executions: dict[str, PurchaseExecution] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, owner: QuoteOwner, quote_id: str) -> PurchaseExecution | None:
        if not quote_id:
            return None
        async with self._guard:
            execution = self._executions.get(quote_id)
            if execution is None:
                return None
            if not _owner_matches(owner, execution):
                # Invisible rather than refused, for the same reason a foreign quote is:
                # nobody may use this server to learn that another client holds this id.
                logger.info("execution_lookup_owner_mismatch")
                return None
            return execution.model_copy(deep=True)

    async def save(self, execution: PurchaseExecution) -> None:
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


def _owner_matches(owner: QuoteOwner, execution: PurchaseExecution) -> bool:
    """Constant-time comparison of both halves of the owner identity."""
    return hmac.compare_digest(owner.session_key, execution.owner_session_key) and hmac.compare_digest(
        owner.user_ref, execution.owner_user_ref
    )


#: Errors a terminal or unresolved execution can replay. Keyed by the stable error code, so a
#: stored record survives a rename of the class and can never replay as a *different* error.
_REPLAYABLE_ERRORS: dict[str, type[EsimMcpError]] = {
    error_type.code: error_type
    for error_type in (
        IdempotencyConflictError,
        InsufficientWalletBalanceError,
        NonWalletQuoteError,
        PurchaseAttemptLimitError,
        PurchaseCurrencyMismatchError,
        PurchaseInProgressError,
        PurchaseManualInterventionError,
        PurchaseOutcomeUnknownError,
        PurchaseRejectedError,
        PurchaseRouteUnavailableError,
        PurchaseUnavailableError,
    )
}


def replay_error(execution: PurchaseExecution) -> EsimMcpError:
    """Rebuild the typed error a non-successful execution recorded.

    An unrecognized stored code degrades to the most cautious answer available rather than to
    a generic internal error: if this server cannot tell what happened, it must not imply that
    nothing did.
    """
    error_type = _REPLAYABLE_ERRORS.get(execution.failure_code or "", PurchaseOutcomeUnknownError)
    return error_type(execution.failure_message or None, details=execution.failure_details or None)


class PurchaseExecutionService:
    """Decide whether a quote's purchase may be sent, and remember what came back.

    Nothing in this class talks to the eSIM backend. It hands the tool layer a key and a
    verdict; the tool layer does the I/O and reports the outcome back here.
    """

    def __init__(self, store: PurchaseExecutionStore) -> None:
        self._store = store

    @property
    def max_attempts(self) -> int:
        return MAX_EXECUTION_ATTEMPTS

    def lock(self, quote_id: str) -> AbstractAsyncContextManager[None]:
        """Serialize everything done for one quote, including the backend call."""
        return self._store.lock(quote_id)

    async def get(self, owner: QuoteOwner, quote_id: str) -> PurchaseExecution | None:
        """Read one owned execution record, or ``None``."""
        return await self._store.get(owner, quote_id)

    async def acquire(self, owner: QuoteOwner, quote_id: str) -> PurchaseExecution:
        """Return the execution for ``quote_id``, minting its key on first use.

        The key is generated exactly once per quote. Every later attempt -- a transport
        retry, a repeated tool call, a recovery after an unknown outcome -- receives the same
        record and therefore the same key.
        """
        existing = await self._store.get(owner, quote_id)
        if existing is not None:
            return existing

        execution = PurchaseExecution(
            quote_id=quote_id,
            owner_session_key=owner.session_key,
            owner_user_ref=owner.user_ref,
            idempotency_key=SecretStr(new_idempotency_key()),
        )
        await self._store.save(execution)
        logger.info(
            "purchase_execution_key_minted",
            extra={"quote_ref": quote_id[:8], "key_fp": key_fingerprint(execution.idempotency_key)},
        )
        return execution

    async def record_attempt(self, execution: PurchaseExecution) -> PurchaseExecution:
        """Count one attempt *before* it is sent, so a crashed call is still counted."""
        updated = execution.model_copy(update={"attempts": execution.attempts + 1, "updated_at": utc_now()})
        await self._store.save(updated)
        return updated

    async def mark_succeeded(self, execution: PurchaseExecution, result: dict[str, Any]) -> PurchaseExecution:
        """Record the one outcome that means money moved and the platform confirmed it."""
        updated = execution.model_copy(
            update={
                "status": ExecutionStatus.SUCCEEDED,
                "result": dict(result),
                "failure_code": None,
                "failure_message": None,
                "failure_details": None,
                "updated_at": utc_now(),
            }
        )
        await self._store.save(updated)
        logger.info(
            "purchase_execution_succeeded",
            extra={"quote_ref": execution.quote_id[:8], "key_fp": key_fingerprint(execution.idempotency_key)},
        )
        return updated

    async def mark_failed(self, execution: PurchaseExecution, error: EsimMcpError) -> PurchaseExecution:
        """Record a final failure. Only used where the platform said nothing was charged."""
        return await self._mark_unsuccessful(execution, error, ExecutionStatus.FAILED)

    async def mark_unresolved(self, execution: PurchaseExecution, error: EsimMcpError) -> PurchaseExecution:
        """Record an outcome this server never learned. The same key may be presented again."""
        return await self._mark_unsuccessful(execution, error, ExecutionStatus.UNRESOLVED)

    async def mark_escalated(self, execution: PurchaseExecution, error: EsimMcpError) -> PurchaseExecution:
        """Record that the platform itself cannot resolve this purchase. Never send it again."""
        return await self._mark_unsuccessful(execution, error, ExecutionStatus.ESCALATED)

    async def invalidate_session(self, session_key: str) -> int:
        """Drop every execution of one MCP session (logout, session invalidation)."""
        dropped = await self._store.invalidate_session(session_key)
        if dropped:
            logger.info("purchase_executions_invalidated_with_session", extra={"dropped_executions": dropped})
        return dropped

    async def _mark_unsuccessful(
        self,
        execution: PurchaseExecution,
        error: EsimMcpError,
        status: ExecutionStatus,
    ) -> PurchaseExecution:
        updated = execution.model_copy(
            update={
                "status": status,
                "failure_code": error.code,
                "failure_message": error.message,
                "failure_details": dict(error.details) if error.details else None,
                "updated_at": utc_now(),
            }
        )
        await self._store.save(updated)
        logger.info(
            "purchase_execution_unsuccessful",
            extra={
                "quote_ref": execution.quote_id[:8],
                "key_fp": key_fingerprint(execution.idempotency_key),
                "execution_status": status.value,
                "error_code": error.code,
            },
        )
        return updated


def require_confirmable_quote(quote: PurchaseQuote) -> PurchaseQuote:
    """Gate a stored quote before any purchase is sent for it.

    Wallet-only by contract in this phase, and the balance snapshot has to have covered the
    price when the quote was made. The platform re-checks both authoritatively -- this is the
    local half, so an obviously impossible purchase is never sent at all.
    """
    if quote.payment_method is not PaymentMethod.WALLET:
        raise NonWalletQuoteError()
    if quote.wallet is None or not quote.wallet.sufficient:
        raise InsufficientWalletBalanceError()
    return quote


def require_attempts_remaining(execution: PurchaseExecution) -> PurchaseExecution:
    """Refuse a further attempt once one quote has been sent too many times."""
    if execution.attempts >= MAX_EXECUTION_ATTEMPTS:
        raise PurchaseAttemptLimitError()
    return execution
