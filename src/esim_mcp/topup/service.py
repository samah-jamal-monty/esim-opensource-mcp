"""Top-up quote lifecycle and checkout bookkeeping. No I/O anywhere in this module.

Three services, all pure domain logic:

* :class:`EsimTopupQuoteService` -- create, read and cancel an MCP-local eSIM top-up quote;
* :class:`WalletTopupQuoteService` -- the same for a wallet top-up amount;
* :class:`WalletTopupCheckoutService` -- decide whether a wallet top-up quote's hosted page
  may be opened at all, and remember what came back so the same link can be handed over
  again instead of asking the platform twice.

Duplicate policy: **supersede**
-------------------------------
Preparing a quote equivalent to an existing active one cancels the older and returns a
brand-new quote, exactly as :mod:`esim_mcp.purchase.service` does and for the same reason: a
quote embeds a price the platform gave a moment ago, and prices go stale. Superseding means
the number a user hears is always the one the platform said seconds ago, and it makes a
repeated preparation harmless rather than an error.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from decimal import Decimal
from typing import Any

from esim_mcp.errors import (
    EsimMcpError,
    EsimTopupAlreadyAttemptedError,
    EsimTopupOutcomeUnknownError,
    EsimTopupRejectedError,
    TooManyActiveQuotesError,
    TopupQuoteCancelledError,
    TopupQuoteExpiredError,
    TopupQuoteNotFoundError,
    WalletTopupNotFoundError,
    WalletTopupOutcomeUnknownError,
    WalletTopupQuoteNotFoundError,
    WalletTopupRejectedError,
    WalletTopupStatusUnavailableError,
    WalletTopupUnavailableError,
)
from esim_mcp.purchase.models import QuotedWallet
from esim_mcp.purchase.store import QuoteOwner
from esim_mcp.settings import Settings
from esim_mcp.topup.models import (
    MAX_TOPUP_CHECKOUT_ATTEMPTS,
    MAX_TOPUP_STATUS_CHECKS,
    EsimTarget,
    EsimTopupExecution,
    EsimTopupExecutionStatus,
    EsimTopupQuote,
    QuotedTopupBundle,
    QuotedTopupPrice,
    TopupQuoteStatus,
    TopupStatus,
    WalletTopupCheckout,
    WalletTopupQuote,
    expiry_from,
    utc_now,
)
from esim_mcp.topup.store import (
    EsimTopupExecutionStore,
    EsimTopupQuoteStore,
    WalletTopupCheckoutStore,
    WalletTopupQuoteStore,
)

logger = logging.getLogger(__name__)

#: Bytes of entropy behind a quote id. Not enumerable, and not derived from anything about
#: the user, the SIM or the amount.
_QUOTE_ID_BYTES = 32


def new_topup_quote_id() -> str:
    """A cryptographically random, opaque quote id.

    Carries no ICCID, email, user id, session key or amount, so it can be shown to the model
    and echoed back without disclosing anything, and it cannot be guessed from a quote the
    caller already holds.
    """
    return secrets.token_urlsafe(_QUOTE_ID_BYTES)


def quote_ref(quote_id: str) -> str:
    """Short, non-reversible reference safe to put in a log record."""
    return quote_id[:8]


def require_usable_topup_quote[QuoteT: (EsimTopupQuote, WalletTopupQuote)](
    quote: QuoteT, *, now: datetime | None = None
) -> QuoteT:
    """Return the quote if it may still be acted on, else raise the matching typed error.

    The single gate both quote kinds pass through, so the read path and the checkout path
    cannot drift apart about what "still usable" means. Expiry is evaluated from the clock
    rather than from the stored status, so a quote whose TTL lapsed without a sweep is
    reported as expired instead of active.
    """
    _require_status(quote.effective_status(now))
    return quote


def _require_status(status: TopupQuoteStatus) -> None:
    """Raise the matching typed error for a quote that may no longer be acted on."""
    if status is TopupQuoteStatus.EXPIRED:
        raise TopupQuoteExpiredError()
    if status is TopupQuoteStatus.CANCELLED:
        raise TopupQuoteCancelledError()
    if status is TopupQuoteStatus.CONSUMED:
        raise TopupQuoteCancelledError(
            "That prepared top-up has already been used and cannot be used again. Prepare it afresh if the user "
            "wants another one."
        )


class EsimTopupQuoteService:
    """Create, read and cancel MCP-local eSIM top-up quotes.

    Nothing in this class talks to the eSIM backend, so no method here can create an order,
    move money, reach a payment provider or touch a provisioned SIM.
    """

    def __init__(self, settings: Settings, store: EsimTopupQuoteStore) -> None:
        self._settings = settings
        self._store = store

    @property
    def ttl_seconds(self) -> int:
        return self._settings.purchase_quote_ttl_seconds

    @property
    def max_active(self) -> int:
        return self._settings.max_active_quotes_per_user

    async def create(
        self,
        owner: QuoteOwner,
        *,
        identity_source: str,
        target: EsimTarget,
        bundle: QuotedTopupBundle,
        price: QuotedTopupPrice,
        payment_method: str,
        wallet: QuotedWallet | None,
        locale: str,
        now: datetime | None = None,
    ) -> EsimTopupQuote:
        """Store a new quote for ``owner``, superseding an equivalent active one.

        "Equivalent" is the same SIM *and* the same top-up plan: a user preparing a second
        top-up for a different SIM keeps both, and one who re-prices the same choice gets a
        fresh number rather than a second quote.
        """
        moment = now or utc_now()
        target_iccid = target.iccid.get_secret_value()

        async with self._store.lock(owner):
            active = await self._store.list_active(owner, now=moment)
            superseded = [
                existing
                for existing in active
                if existing.target.iccid.get_secret_value() == target_iccid
                and existing.bundle.bundle_code == bundle.bundle_code
            ]
            for existing in superseded:
                await self._store.set_status(owner, existing.quote_id, TopupQuoteStatus.CANCELLED)
                logger.info("esim_topup_quote_superseded", extra={"quote_ref": quote_ref(existing.quote_id)})

            remaining = len(active) - len(superseded)
            if remaining >= self.max_active:
                logger.info("esim_topup_quote_limit_reached", extra={"active_quotes": remaining})
                raise TooManyActiveQuotesError(
                    f"This client already has {remaining} prepared top-ups, which is the maximum. Ask the user "
                    "which one they no longer want and cancel it, or wait for one to expire."
                )

            quote = EsimTopupQuote(
                quote_id=new_topup_quote_id(),
                owner_session_key=owner.session_key,
                owner_user_ref=owner.user_ref,
                identity_source=identity_source,
                target=target,
                bundle=bundle,
                price=price,
                payment_method=payment_method,
                wallet=wallet,
                locale=locale,
                created_at=moment,
                expires_at=expiry_from(moment, self.ttl_seconds),
            )
            await self._store.save(quote)

        logger.info(
            "esim_topup_quote_prepared",
            extra={
                "quote_ref": quote_ref(quote.quote_id),
                # Never the ICCID, never the amount: a log line is not a receipt.
                "order_created": False,
                "charged": False,
                "topped_up": False,
            },
        )
        return quote

    async def get(self, owner: QuoteOwner, quote_id: str) -> EsimTopupQuote:
        """Return one usable quote owned by ``owner``, else raise a typed error."""
        quote = await self.get_record(owner, quote_id)
        _require_status(quote.effective_status())
        return quote

    async def get_record(self, owner: QuoteOwner, quote_id: str) -> EsimTopupQuote:
        """Return one owned quote **whatever its status**, for honest state reporting."""
        quote = await self._store.get(owner, quote_id)
        if quote is None:
            raise TopupQuoteNotFoundError()
        return quote

    async def cancel(self, owner: QuoteOwner, quote_id: str, *, now: datetime | None = None) -> EsimTopupQuote:
        """Cancel one owned quote. Local, idempotent, and with nothing to undo at the platform."""
        quote = await self.get_record(owner, quote_id)
        if quote.effective_status(now or utc_now()) is not TopupQuoteStatus.ACTIVE:
            return quote
        updated = await self._store.set_status(owner, quote_id, TopupQuoteStatus.CANCELLED)
        if updated is None:  # pragma: no cover - lost race with an invalidation
            raise TopupQuoteNotFoundError()
        logger.info("esim_topup_quote_cancelled", extra={"quote_ref": quote_ref(quote_id)})
        return updated

    async def consume(self, owner: QuoteOwner, quote_id: str) -> EsimTopupQuote:
        """Mark one owned quote as spent, so it can never become a second top-up.

        Called from exactly one place: immediately after the platform confirmed a completed
        top-up. It is deliberately *not* called for a failed, ambiguous or unknown outcome --
        consuming a quote whose top-up may not have happened would destroy the only record of
        what the user agreed to. The execution record locks those cases instead.
        """
        quote = await self.get_record(owner, quote_id)
        updated = await self._store.set_status(owner, quote_id, TopupQuoteStatus.CONSUMED)
        if updated is None:  # pragma: no cover - lost race with an invalidation
            raise TopupQuoteNotFoundError()
        logger.info("esim_topup_quote_consumed", extra={"quote_ref": quote_ref(quote.quote_id)})
        return updated

    async def invalidate_session(self, session_key: str) -> int:
        """Cancel every quote held under one MCP session key (logout, session invalidation).

        A top-up quote naming somebody's SIM must not outlive their session: whoever signs in
        next on the same MCP client would otherwise hold a reference to another user's eSIM.
        """
        cancelled = await self._store.invalidate_session(session_key)
        if cancelled:
            logger.info("esim_topup_quotes_invalidated_with_session", extra={"cancelled_quotes": cancelled})
        return cancelled


#: Errors an already-attempted execution can replay. Keyed by the stable error code, so a
#: stored record survives a rename of the class and can never replay as a *different* error.
_REPLAYABLE_EXECUTION_ERRORS: dict[str, type[EsimMcpError]] = {
    error_type.code: error_type
    for error_type in (
        EsimTopupAlreadyAttemptedError,
        EsimTopupOutcomeUnknownError,
        EsimTopupRejectedError,
    )
}


def replay_execution_error(execution: EsimTopupExecution) -> EsimMcpError:
    """Rebuild the typed error a non-successful execution recorded.

    An unrecognized stored code degrades to :class:`EsimTopupOutcomeUnknownError` rather
    than to a refusal: if this server cannot tell what happened to a non-idempotent write,
    it must not imply that nothing did.
    """
    error_type = _REPLAYABLE_EXECUTION_ERRORS.get(
        execution.failure_code or "", EsimTopupOutcomeUnknownError
    )
    return error_type(
        execution.failure_message or None, details=dict(execution.failure_details or {}) or None
    )


class EsimTopupExecutionService:
    """Decide whether a quote's top-up may be sent **at all**, and remember that it was.

    Nothing in this class talks to the eSIM backend. It hands the tool layer a verdict; the
    tool layer does the I/O and reports the outcome back here.

    The rule it enforces is one line long and is the whole of the safety story for a
    non-idempotent write: **one quote, one attempt, ever.** Not "one attempt per outcome",
    not "retry if unresolved" -- one. A caller that wants to try again has to prepare a fresh
    quote, which is a deliberate act by the user rather than a retry by the model.
    """

    def __init__(self, store: EsimTopupExecutionStore) -> None:
        self._store = store

    def lock(self, quote_id: str) -> AbstractAsyncContextManager[None]:
        """Serialize everything done for one quote, including the backend call."""
        return self._store.lock(quote_id)

    async def get(self, owner: QuoteOwner, quote_id: str) -> EsimTopupExecution | None:
        return await self._store.get(owner, quote_id)

    async def acquire(self, owner: QuoteOwner, quote_id: str) -> EsimTopupExecution:
        """Return the execution record for ``quote_id``, creating it on first use."""
        existing = await self._store.get(owner, quote_id)
        if existing is not None:
            return existing
        execution = EsimTopupExecution(
            quote_id=quote_id,
            owner_session_key=owner.session_key,
            owner_user_ref=owner.user_ref,
        )
        await self._store.save(execution)
        return execution

    async def record_attempt(self, execution: EsimTopupExecution) -> EsimTopupExecution:
        """Count the attempt *before* it is sent, so a request that dies still counts.

        This ordering is load-bearing. If the process is interrupted between here and the
        response, the quote is already locked -- which is the answer that cannot charge
        anybody twice.
        """
        return await self._save(execution, attempts=execution.attempts + 1)

    async def mark_succeeded(
        self, execution: EsimTopupExecution, result: Mapping[str, Any]
    ) -> EsimTopupExecution:
        """Record the one outcome that means the platform confirmed the top-up."""
        updated = await self._save(
            execution,
            status=EsimTopupExecutionStatus.SUCCEEDED,
            result=dict(result),
            failure_code=None,
            failure_message=None,
            failure_details=None,
        )
        logger.info(
            "esim_topup_execution_succeeded",
            extra={"quote_ref": quote_ref(execution.quote_id), "charged": True, "topped_up": True},
        )
        return updated

    async def mark_failed(
        self, execution: EsimTopupExecution, error: EsimMcpError
    ) -> EsimTopupExecution:
        """Record a definitive refusal, where the platform said nothing was charged."""
        return await self._mark_unsuccessful(execution, error, EsimTopupExecutionStatus.FAILED)

    async def mark_unresolved(
        self, execution: EsimTopupExecution, error: EsimMcpError
    ) -> EsimTopupExecution:
        """Record an outcome this server never learned. The quote is locked for good."""
        return await self._mark_unsuccessful(execution, error, EsimTopupExecutionStatus.UNRESOLVED)

    async def invalidate_session(self, session_key: str) -> int:
        dropped = await self._store.invalidate_session(session_key)
        if dropped:
            logger.info("esim_topup_executions_invalidated_with_session", extra={"dropped": dropped})
        return dropped

    # ------------------------------------------------------------------ internals

    async def _save(self, execution: EsimTopupExecution, **updates: Any) -> EsimTopupExecution:
        updated = execution.model_copy(update={**updates, "updated_at": utc_now()})
        await self._store.save(updated)
        return updated

    async def _mark_unsuccessful(
        self,
        execution: EsimTopupExecution,
        error: EsimMcpError,
        status: EsimTopupExecutionStatus,
    ) -> EsimTopupExecution:
        updated = await self._save(
            execution,
            status=status,
            failure_code=error.code,
            failure_message=error.message,
            failure_details=dict(error.details) if error.details else None,
        )
        logger.info(
            "esim_topup_execution_unsuccessful",
            extra={
                "quote_ref": quote_ref(execution.quote_id),
                "execution_status": status.value,
                "error_code": error.code,
                # Only a definitive refusal may claim nothing was charged.
                "charged": None if status is EsimTopupExecutionStatus.UNRESOLVED else False,
            },
        )
        return updated


def require_never_attempted(execution: EsimTopupExecution) -> EsimTopupExecution:
    """Refuse a quote that has already been sent, whatever the outcome was.

    The single gate that makes a non-idempotent write survivable. It is checked *after* the
    stored answer is replayed, so a caller asking about a successful top-up gets the receipt
    rather than this refusal.
    """
    if execution.was_sent:
        raise EsimTopupAlreadyAttemptedError()
    return execution


class WalletTopupQuoteService:
    """Create, read and cancel MCP-local wallet top-up quotes."""

    def __init__(self, settings: Settings, store: WalletTopupQuoteStore) -> None:
        self._settings = settings
        self._store = store

    @property
    def ttl_seconds(self) -> int:
        return self._settings.purchase_quote_ttl_seconds

    @property
    def max_active(self) -> int:
        return self._settings.max_active_quotes_per_user

    async def create(
        self,
        owner: QuoteOwner,
        *,
        identity_source: str,
        amount: Decimal,
        currency: str,
        balance: Decimal | None,
        minimum_amount: Decimal | None,
        maximum_amount: Decimal | None,
        locale: str,
        now: datetime | None = None,
    ) -> WalletTopupQuote:
        """Store a new quote for ``owner``, superseding an equivalent active one."""
        moment = now or utc_now()

        async with self._store.lock(owner):
            active = await self._store.list_active(owner, now=moment)
            superseded = [
                existing
                for existing in active
                if existing.amount == amount and existing.currency == currency
            ]
            for existing in superseded:
                await self._store.set_status(owner, existing.quote_id, TopupQuoteStatus.CANCELLED)
                logger.info("wallet_topup_quote_superseded", extra={"quote_ref": quote_ref(existing.quote_id)})

            remaining = len(active) - len(superseded)
            if remaining >= self.max_active:
                raise TooManyActiveQuotesError(
                    f"This client already has {remaining} prepared top-ups, which is the maximum. Ask the user "
                    "which one they no longer want and cancel it, or wait for one to expire."
                )

            quote = WalletTopupQuote(
                quote_id=new_topup_quote_id(),
                owner_session_key=owner.session_key,
                owner_user_ref=owner.user_ref,
                identity_source=identity_source,
                amount=amount,
                currency=currency,
                balance=balance,
                minimum_amount=minimum_amount,
                maximum_amount=maximum_amount,
                locale=locale,
                created_at=moment,
                expires_at=expiry_from(moment, self.ttl_seconds),
            )
            await self._store.save(quote)

        logger.info(
            "wallet_topup_quote_prepared",
            extra={"quote_ref": quote_ref(quote.quote_id), "order_created": False, "charged": False},
        )
        return quote

    async def get(self, owner: QuoteOwner, quote_id: str) -> WalletTopupQuote:
        quote = await self.get_record(owner, quote_id)
        _require_status(quote.effective_status())
        return quote

    async def get_record(self, owner: QuoteOwner, quote_id: str) -> WalletTopupQuote:
        quote = await self._store.get(owner, quote_id)
        if quote is None:
            raise WalletTopupQuoteNotFoundError()
        return quote

    async def consume(self, owner: QuoteOwner, quote_id: str) -> WalletTopupQuote:
        """Mark one owned quote as spent, so it can never open a second payment page.

        Called from exactly one place: after the platform confirms the top-up was paid.
        Never for a pending, failed or unknown outcome -- consuming a quote whose payment
        may not have happened would destroy the record of what the user agreed to.
        """
        quote = await self.get_record(owner, quote_id)
        updated = await self._store.set_status(owner, quote_id, TopupQuoteStatus.CONSUMED)
        if updated is None:  # pragma: no cover - lost race with an invalidation
            raise WalletTopupQuoteNotFoundError()
        logger.info("wallet_topup_quote_consumed", extra={"quote_ref": quote_ref(quote.quote_id)})
        return updated

    async def invalidate_session(self, session_key: str) -> int:
        cancelled = await self._store.invalidate_session(session_key)
        if cancelled:
            logger.info("wallet_topup_quotes_invalidated_with_session", extra={"cancelled_quotes": cancelled})
        return cancelled


#: Errors a terminal checkout can replay. Keyed by the stable error code, so a stored record
#: survives a rename of the class and can never replay as a *different* error.
_REPLAYABLE_ERRORS: dict[str, type[EsimMcpError]] = {
    error_type.code: error_type
    for error_type in (
        WalletTopupNotFoundError,
        WalletTopupOutcomeUnknownError,
        WalletTopupRejectedError,
        WalletTopupStatusUnavailableError,
        WalletTopupUnavailableError,
    )
}


def replay_error(checkout: WalletTopupCheckout) -> EsimMcpError:
    """Rebuild the typed error a failed checkout recorded.

    An unrecognized stored code degrades to a plain refusal rather than to a generic internal
    error: a page that did not open charged nobody, so "it did not start" is both the safest
    and the most accurate thing left to say.
    """
    error_type = _REPLAYABLE_ERRORS.get(checkout.failure_code or "", WalletTopupRejectedError)
    return error_type(checkout.failure_message or None, details=dict(checkout.failure_details or {}) or None)


class WalletTopupCheckoutService:
    """Decide whether a quote's top-up page may be opened, and remember what came back.

    Nothing in this class talks to the eSIM backend. It hands the tool layer a verdict; the
    tool layer does the I/O and reports the outcome back here.

    .. important::
       Unlike :class:`~esim_mcp.purchase.card.CardCheckoutService` this class mints **no
       idempotency key**, and that omission is deliberate. Duplicate protection for a wallet
       top-up is the platform's own pending-order reuse, which is durable across a restart of
       this process; a key held here would look like a guarantee and stop being one the
       moment the process moved. What is kept here is a link to replay and an ownership
       boundary for a payment reference -- both conveniences, neither load-bearing.
    """

    def __init__(self, store: WalletTopupCheckoutStore) -> None:
        self._store = store

    @property
    def max_attempts(self) -> int:
        return MAX_TOPUP_CHECKOUT_ATTEMPTS

    @property
    def max_checks(self) -> int:
        return MAX_TOPUP_STATUS_CHECKS

    def lock(self, key: str) -> AbstractAsyncContextManager[None]:
        """Serialize everything done for one quote, including the backend call."""
        return self._store.lock(key)

    async def get(self, owner: QuoteOwner, quote_id: str) -> WalletTopupCheckout | None:
        return await self._store.get(owner, quote_id)

    async def require_by_reference(self, owner: QuoteOwner, payment_reference: str) -> WalletTopupCheckout:
        """Resolve a payment reference **within this owner**, or refuse it as unknown.

        This is the ownership boundary for ``get_wallet_topup_status``: a reference this
        client never received is indistinguishable from one that does not exist, so nothing
        here can be used to probe for another client's top-ups. The platform enforces the
        same rule independently, by scoping its own lookup to the authenticated user.
        """
        checkout = await self._store.get_by_reference(owner, payment_reference)
        if checkout is None:
            raise WalletTopupNotFoundError()
        return checkout

    async def acquire(self, owner: QuoteOwner, quote_id: str) -> WalletTopupCheckout:
        """Return the checkout record for ``quote_id``, creating it on first use."""
        existing = await self._store.get(owner, quote_id)
        if existing is not None:
            return existing
        checkout = WalletTopupCheckout(
            quote_id=quote_id,
            owner_session_key=owner.session_key,
            owner_user_ref=owner.user_ref,
        )
        await self._store.save(checkout)
        return checkout

    async def record_attempt(self, checkout: WalletTopupCheckout) -> WalletTopupCheckout:
        """Count one attempt *before* it is sent, so a crashed call is still counted."""
        return await self._save(checkout, attempts=checkout.attempts + 1)

    async def record_check(self, checkout: WalletTopupCheckout) -> WalletTopupCheckout:
        """Count one status read *before* it is sent."""
        return await self._save(checkout, checks=checkout.checks + 1)

    async def mark_open(
        self,
        checkout: WalletTopupCheckout,
        *,
        result: Mapping[str, Any],
        payment_reference: str,
        locale: str,
        currency: str,
    ) -> WalletTopupCheckout:
        """Record that a hosted page exists. This is not a payment and not a credit."""
        updated = await self._save(
            checkout,
            status=TopupStatus.OPEN,
            result=dict(result),
            payment_reference=payment_reference,
            locale=locale,
            currency=currency,
            failure_code=None,
            failure_message=None,
            failure_details=None,
        )
        logger.info(
            "wallet_topup_checkout_opened",
            extra={
                "quote_ref": quote_ref(checkout.quote_id),
                # A page is not money. This stays false here and everywhere in this module,
                # because nothing in it can charge anyone or credit a balance.
                "charged": False,
                "credited": False,
            },
        )
        return updated

    async def mark_failed(self, checkout: WalletTopupCheckout, error: EsimMcpError) -> WalletTopupCheckout:
        """Record a final refusal. Only used where no payment page was opened."""
        return await self._mark_unsuccessful(checkout, error, TopupStatus.FAILED)

    async def mark_unresolved(self, checkout: WalletTopupCheckout, error: EsimMcpError) -> WalletTopupCheckout:
        """Record an answer this server never learned. Asking again is safe and encouraged."""
        return await self._mark_unsuccessful(checkout, error, TopupStatus.UNRESOLVED)

    async def mark_settled(
        self, checkout: WalletTopupCheckout, result: Mapping[str, Any]
    ) -> WalletTopupCheckout:
        """Store a *terminal* status so later checks replay it instead of re-reading."""
        return await self._save(checkout, payment_result=dict(result))

    async def invalidate_session(self, session_key: str) -> int:
        dropped = await self._store.invalidate_session(session_key)
        if dropped:
            logger.info("wallet_topup_checkouts_invalidated_with_session", extra={"dropped_checkouts": dropped})
        return dropped

    # ------------------------------------------------------------------ internals

    async def _save(self, checkout: WalletTopupCheckout, **updates: Any) -> WalletTopupCheckout:
        updated = checkout.model_copy(update={**updates, "updated_at": utc_now()})
        await self._store.save(updated)
        return updated

    async def _mark_unsuccessful(
        self,
        checkout: WalletTopupCheckout,
        error: EsimMcpError,
        status: TopupStatus,
    ) -> WalletTopupCheckout:
        updated = await self._save(
            checkout,
            status=status,
            failure_code=error.code,
            failure_message=error.message,
            failure_details=dict(error.details) if error.details else None,
        )
        logger.info(
            "wallet_topup_checkout_unsuccessful",
            extra={
                "quote_ref": quote_ref(checkout.quote_id),
                "checkout_status": status.value,
                "error_code": error.code,
                "charged": False,
                "credited": False,
            },
        )
        return updated


def require_attempts_remaining(checkout: WalletTopupCheckout) -> WalletTopupCheckout:
    """Refuse a further attempt once one quote has been sent too many times."""
    if checkout.attempts >= MAX_TOPUP_CHECKOUT_ATTEMPTS:
        raise WalletTopupRejectedError(
            "Opening a payment page for this top-up has been attempted the maximum number of times without a "
            "clear result. Do NOT try again. Nothing was charged: tell the user, and offer to try a fresh "
            "top-up or to contact eSIM support."
        )
    return checkout


def require_checks_remaining(checkout: WalletTopupCheckout) -> WalletTopupCheckout:
    """Refuse a further status read once one top-up has been checked too many times."""
    if checkout.checks >= MAX_TOPUP_STATUS_CHECKS:
        raise WalletTopupStatusUnavailableError(
            "This top-up has been checked the maximum number of times without reaching a final state. Stop "
            "checking it. Tell the user the platform has not settled it yet and that they should check their "
            "eSIM app or contact eSIM support."
        )
    return checkout
