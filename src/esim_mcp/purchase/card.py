"""Card-checkout bookkeeping: one idempotency key per quote, and one payment page per quote.

The card sibling of :mod:`esim_mcp.purchase.execution`, and like it, this module performs
**no I/O**. What it owns is the decision of *whether a checkout may be opened at all*, what to
say when one already was, and which client is allowed to ask after a given payment.

The rules it enforces
---------------------
* **One key per quote, minted once.** :meth:`CardCheckoutService.acquire` mints a
  cryptographically random ``Idempotency-Key`` the first time a quote is sent for checkout and
  returns that same key for every later attempt on that quote. A transport retry, a reconnect
  or a model that calls the tool twice therefore all present the platform with the *same* key,
  which is what lets it hand back the existing payment page instead of opening a second one.
  A new key is only ever produced by a new quote.
* **An open page is replayed, never re-created.** Once a checkout exists for a quote, its
  stored result is returned verbatim and no request is sent -- so a user is never given two
  links for one plan, and never pays twice for wanting to see the link again.
* **A payment belongs to the client that started it.** A payment reference is only resolvable
  through the owner that created it, so this server cannot be used to read, or even to
  confirm the existence of, somebody else's payment.
* **Checking is bounded.** Attempts and status checks are capped
  (:data:`MAX_CHECKOUT_ATTEMPTS`, :data:`MAX_STATUS_CHECKS`) so neither becomes an unattended
  polling loop against the platform.

How this differs from the wallet ledger, deliberately
-----------------------------------------------------
Opening a checkout page **moves no money**: the card is entered on the payment provider's own
hosted page, which this server never sees and cannot submit to. So an unclear answer *while
creating a page* is honestly reportable as "the link could not be opened", and the same key
may safely be presented again. The dangerous ambiguity in this phase lives on the other side
-- in the payment *status* -- which is why an ambiguous status is terminal here and stops
further checks, exactly as an escalated wallet purchase does.

.. warning::
   :class:`InMemoryCardCheckoutStore` keeps records in the process heap. If this server
   restarts, the local memory of an open checkout is lost: the platform's own record is not,
   so the user's payment is unaffected and the same key would still resolve there, but this
   server can no longer replay the link or check the payment. A durable (encrypted Redis)
   store is the same seam as for quotes and executions.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from esim_mcp.errors import (
    CardCheckoutAttemptLimitError,
    CardCheckoutOutcomeUnknownError,
    CardCheckoutRejectedError,
    CardCheckoutUnavailableError,
    CardPaymentAmbiguousError,
    CardPaymentCheckLimitError,
    CardPaymentNotFoundError,
    CardPaymentStatusUnavailableError,
    EsimMcpError,
    NonCardQuoteError,
    UnsafeCheckoutLinkError,
)
from esim_mcp.purchase.execution import key_fingerprint, new_idempotency_key
from esim_mcp.purchase.models import PaymentMethod, PurchaseQuote, utc_now
from esim_mcp.purchase.store import QuoteOwner

logger = logging.getLogger(__name__)

#: How many times one quote may be sent for checkout before this server refuses to try again.
#: Every attempt after the first carries the *same* key, so this guards against an unattended
#: retry loop rather than against a duplicate page.
MAX_CHECKOUT_ATTEMPTS = 3

#: How many times one payment may be checked before this server stops asking. Terminal
#: answers are replayed from the local record and never count, so this bound only ever
#: applies to a payment the platform has genuinely not settled.
MAX_STATUS_CHECKS = 20


class CheckoutStatus(StrEnum):
    """Lifecycle of one quote's checkout page.

    ``OPEN`` is terminal for *creation*: a page exists, so the stored answer is replayed and
    nothing is sent. It says nothing at all about whether the user paid -- only
    :class:`~esim_mcp.models.card.CardPaymentStatus`, read from the platform, can say that.
    """

    PENDING = "pending"
    OPEN = "open"
    FAILED = "failed"
    UNRESOLVED = "unresolved"

    @property
    def is_terminal(self) -> bool:
        """True when the stored answer must be replayed rather than sent again."""
        return self in (CheckoutStatus.OPEN, CheckoutStatus.FAILED)


class CardCheckout(BaseModel):
    """One quote's checkout history, including the key it is bound to.

    ``model_config`` is ``extra="forbid"``: no amount taken from a caller, no card detail, no
    provider session id, no client secret and no token can be added to this record by an
    unvalidated payload. The idempotency key is the only secret it holds and it is a
    :class:`~pydantic.SecretStr`, so it cannot be logged or serialized by accident.
    """

    model_config = ConfigDict(extra="forbid")

    quote_id: str
    owner_session_key: str
    owner_user_ref: str
    idempotency_key: SecretStr
    attempts: int = 0
    checks: int = 0
    status: CheckoutStatus = CheckoutStatus.PENDING
    #: The safe checkout payload replayed to the caller once a page exists.
    result: dict[str, Any] | None = None
    #: The platform's reference for the payment, used for every later status read.
    payment_reference: str | None = None
    #: The locale and currency the quote was priced in. Carried so a later status read asks
    #: the platform in the same terms the user was quoted in, rather than in server defaults
    #: that might render a different amount back to them.
    locale: str | None = None
    currency: str | None = None
    #: The safe payload of a *terminal* payment status, replayed instead of re-read.
    payment_result: dict[str, Any] | None = None
    #: The typed error a terminal failure replays.
    failure_code: str | None = None
    failure_message: str | None = None
    failure_details: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def attempts_remaining(self) -> int:
        return max(0, MAX_CHECKOUT_ATTEMPTS - self.attempts)

    @property
    def checks_remaining(self) -> int:
        return max(0, MAX_STATUS_CHECKS - self.checks)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CardCheckout(quote={self.quote_id[:8]}..., status={self.status.value}, attempts={self.attempts})"


class CardCheckoutStore(ABC):
    """Storage contract for card checkouts, scoped to one owner exactly like quotes."""

    @abstractmethod
    async def get(self, owner: QuoteOwner, quote_id: str) -> CardCheckout | None:
        """Return the checkout if it exists **and** belongs to ``owner``, else ``None``."""

    @abstractmethod
    async def get_by_reference(self, owner: QuoteOwner, payment_reference: str) -> CardCheckout | None:
        """Return the checkout for one payment reference, scoped to ``owner``.

        A reference belonging to another client is *not found* rather than refused, so this
        server cannot be used to discover that somebody else's payment exists.
        """

    @abstractmethod
    async def save(self, checkout: CardCheckout) -> None:
        """Insert or replace one checkout record."""

    @abstractmethod
    async def invalidate_session(self, session_key: str) -> int:
        """Drop every checkout held under one MCP session key. Returns how many were dropped."""

    @abstractmethod
    def lock(self, key: str) -> AbstractAsyncContextManager[None]:
        """Mutual exclusion for one quote or one payment, held across the whole call.

        Deliberately held across backend I/O: two concurrent calls for the same quote must
        not both reach the platform, whatever order the client sends them in.
        """

    async def aclose(self) -> None:
        """Release any backend resources. No-op by default."""
        return None


class InMemoryCardCheckoutStore(CardCheckoutStore):
    """Process-local checkout store. Local/single-instance operation only."""

    def __init__(self) -> None:
        self._checkouts: dict[str, CardCheckout] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, owner: QuoteOwner, quote_id: str) -> CardCheckout | None:
        if not quote_id:
            return None
        async with self._guard:
            checkout = self._checkouts.get(quote_id)
            if checkout is None:
                return None
            if not _owner_matches(owner, checkout):
                logger.info("card_checkout_lookup_owner_mismatch")
                return None
            return checkout.model_copy(deep=True)

    async def get_by_reference(self, owner: QuoteOwner, payment_reference: str) -> CardCheckout | None:
        if not payment_reference:
            return None
        async with self._guard:
            for checkout in self._checkouts.values():
                if checkout.payment_reference is None:
                    continue
                if not hmac.compare_digest(checkout.payment_reference, payment_reference):
                    continue
                if not _owner_matches(owner, checkout):
                    logger.info("card_payment_lookup_owner_mismatch")
                    return None
                return checkout.model_copy(deep=True)
            return None

    async def save(self, checkout: CardCheckout) -> None:
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


def _owner_matches(owner: QuoteOwner, checkout: CardCheckout) -> bool:
    """Constant-time comparison of both halves of the owner identity."""
    return hmac.compare_digest(owner.session_key, checkout.owner_session_key) and hmac.compare_digest(
        owner.user_ref, checkout.owner_user_ref
    )


#: Errors a terminal checkout can replay. Keyed by the stable error code, so a stored record
#: survives a rename of the class and can never replay as a *different* error.
_REPLAYABLE_ERRORS: dict[str, type[EsimMcpError]] = {
    error_type.code: error_type
    for error_type in (
        CardCheckoutAttemptLimitError,
        CardCheckoutOutcomeUnknownError,
        CardCheckoutRejectedError,
        CardCheckoutUnavailableError,
        CardPaymentAmbiguousError,
        CardPaymentCheckLimitError,
        CardPaymentStatusUnavailableError,
        NonCardQuoteError,
        UnsafeCheckoutLinkError,
    )
}


def replay_error(checkout: CardCheckout) -> EsimMcpError:
    """Rebuild the typed error a failed checkout recorded.

    An unrecognized stored code degrades to a plain refusal rather than to a generic internal
    error: a checkout that did not open charged nobody, so "it did not start" is both the
    safest and the most accurate thing left to say.
    """
    error_type = _REPLAYABLE_ERRORS.get(checkout.failure_code or "", CardCheckoutRejectedError)
    return error_type(checkout.failure_message or None, details=checkout.failure_details or None)


class CardCheckoutService:
    """Decide whether a quote's checkout may be opened, and remember what came back.

    Nothing in this class talks to the eSIM backend. It hands the tool layer a key and a
    verdict; the tool layer does the I/O and reports the outcome back here.
    """

    def __init__(self, store: CardCheckoutStore) -> None:
        self._store = store

    @property
    def max_attempts(self) -> int:
        return MAX_CHECKOUT_ATTEMPTS

    @property
    def max_checks(self) -> int:
        return MAX_STATUS_CHECKS

    def lock(self, key: str) -> AbstractAsyncContextManager[None]:
        """Serialize everything done for one quote, including the backend call."""
        return self._store.lock(key)

    async def get(self, owner: QuoteOwner, quote_id: str) -> CardCheckout | None:
        """Read one owned checkout record, or ``None``."""
        return await self._store.get(owner, quote_id)

    async def require_by_reference(self, owner: QuoteOwner, payment_reference: str) -> CardCheckout:
        """Resolve a payment reference **within this owner**, or refuse it as unknown.

        This is the ownership boundary for :func:`check_card_payment_status`: a reference this
        client never received is indistinguishable from one that does not exist, so nothing
        here can be used to probe for another client's payments.
        """
        checkout = await self._store.get_by_reference(owner, payment_reference)
        if checkout is None:
            raise CardPaymentNotFoundError()
        return checkout

    async def acquire(self, owner: QuoteOwner, quote_id: str) -> CardCheckout:
        """Return the checkout for ``quote_id``, minting its key on first use.

        The key is generated exactly once per quote. Every later attempt -- a transport retry,
        a repeated tool call, a recovery after an unclear answer -- receives the same record
        and therefore the same key, which is what makes a second payment page impossible.
        """
        existing = await self._store.get(owner, quote_id)
        if existing is not None:
            return existing

        checkout = CardCheckout(
            quote_id=quote_id,
            owner_session_key=owner.session_key,
            owner_user_ref=owner.user_ref,
            idempotency_key=SecretStr(new_idempotency_key()),
        )
        await self._store.save(checkout)
        logger.info(
            "card_checkout_key_minted",
            extra={"quote_ref": quote_id[:8], "key_fp": key_fingerprint(checkout.idempotency_key)},
        )
        return checkout

    async def record_attempt(self, checkout: CardCheckout) -> CardCheckout:
        """Count one attempt *before* it is sent, so a crashed call is still counted."""
        return await self._save(checkout, attempts=checkout.attempts + 1)

    async def record_check(self, checkout: CardCheckout) -> CardCheckout:
        """Count one status read *before* it is sent."""
        return await self._save(checkout, checks=checkout.checks + 1)

    async def mark_open(
        self,
        checkout: CardCheckout,
        *,
        result: dict[str, Any],
        payment_reference: str,
        locale: str,
        currency: str,
    ) -> CardCheckout:
        """Record that a hosted payment page exists. This is not a payment."""
        updated = await self._save(
            checkout,
            status=CheckoutStatus.OPEN,
            result=dict(result),
            payment_reference=payment_reference,
            locale=locale,
            currency=currency,
            failure_code=None,
            failure_message=None,
            failure_details=None,
        )
        logger.info(
            "card_checkout_opened",
            extra={
                "quote_ref": checkout.quote_id[:8],
                "key_fp": key_fingerprint(checkout.idempotency_key),
                # A page is not money: this stays false here, and it stays false everywhere in
                # this module, because nothing in it can charge anyone. The platform may have
                # recorded an *unpaid* order behind the page; only its webhook can settle one.
                "charged": False,
                "provisioned": False,
            },
        )
        return updated

    async def mark_failed(self, checkout: CardCheckout, error: EsimMcpError) -> CardCheckout:
        """Record a final refusal. Only used where no payment page was opened."""
        return await self._mark_unsuccessful(checkout, error, CheckoutStatus.FAILED)

    async def mark_unresolved(self, checkout: CardCheckout, error: EsimMcpError) -> CardCheckout:
        """Record an answer this server never learned. The same key may be presented again."""
        return await self._mark_unsuccessful(checkout, error, CheckoutStatus.UNRESOLVED)

    async def mark_payment_settled(self, checkout: CardCheckout, result: dict[str, Any]) -> CardCheckout:
        """Store a *terminal* payment status so later checks replay it instead of re-reading."""
        return await self._save(checkout, payment_result=dict(result))

    async def invalidate_session(self, session_key: str) -> int:
        """Drop every checkout of one MCP session (logout, session invalidation)."""
        dropped = await self._store.invalidate_session(session_key)
        if dropped:
            logger.info("card_checkouts_invalidated_with_session", extra={"dropped_checkouts": dropped})
        return dropped

    # ------------------------------------------------------------------ internals

    async def _save(self, checkout: CardCheckout, **updates: Any) -> CardCheckout:
        updated = checkout.model_copy(update={**updates, "updated_at": utc_now()})
        await self._store.save(updated)
        return updated

    async def _mark_unsuccessful(
        self,
        checkout: CardCheckout,
        error: EsimMcpError,
        status: CheckoutStatus,
    ) -> CardCheckout:
        updated = await self._save(
            checkout,
            status=status,
            failure_code=error.code,
            failure_message=error.message,
            failure_details=dict(error.details) if error.details else None,
        )
        logger.info(
            "card_checkout_unsuccessful",
            extra={
                "quote_ref": checkout.quote_id[:8],
                "key_fp": key_fingerprint(checkout.idempotency_key),
                "checkout_status": status.value,
                "error_code": error.code,
                "charged": False,
            },
        )
        return updated


def require_card_quote(quote: PurchaseQuote) -> PurchaseQuote:
    """Gate a stored quote before any checkout is opened for it.

    Card-only by contract: a wallet quote has a balance snapshot behind it and belongs to
    ``confirm_purchase``, and silently routing one through a card page would charge the user
    by a method they did not choose.
    """
    if quote.payment_method is not PaymentMethod.CARD:
        raise NonCardQuoteError()
    return quote


def require_attempts_remaining(checkout: CardCheckout) -> CardCheckout:
    """Refuse a further attempt once one quote has been sent too many times."""
    if checkout.attempts >= MAX_CHECKOUT_ATTEMPTS:
        raise CardCheckoutAttemptLimitError()
    return checkout


def require_checks_remaining(checkout: CardCheckout) -> CardCheckout:
    """Refuse a further status read once one payment has been checked too many times."""
    if checkout.checks >= MAX_STATUS_CHECKS:
        raise CardPaymentCheckLimitError()
    return checkout
