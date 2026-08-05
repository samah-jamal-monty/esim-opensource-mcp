"""Quote lifecycle orchestration: create, read, cancel.

This layer owns the *rules* about quotes and nothing else. It performs no backend I/O and
knows nothing about MCP: the tool layer fetches authoritative bundle and wallet facts and
hands them in, and this service decides whether a quote may be created, which existing quote
it replaces, and whether a stored quote may still be read.

Duplicate policy: **supersede**
-------------------------------
Preparing a quote for the same plan *and* the same payment method as an existing active quote
**cancels the older one** and returns a brand-new quote. The alternative -- handing back the
existing quote -- was rejected deliberately: a quote embeds a catalogue price, a wallet
balance and an availability check, and all three go stale. Returning a five-minute-old quote
would risk reading out a price the platform no longer offers. Superseding means the numbers a
user hears are always the ones the platform reported seconds ago, and it makes a repeated
call harmless rather than an error, so an assistant that re-prepares the same choice does not
accumulate quotes or trip the per-user limit.

The whole read-supersede-count-insert sequence runs under a per-owner lock, so two concurrent
preparations for the same plan cannot both end up active.
"""

from __future__ import annotations

import logging
from datetime import datetime

from esim_mcp.errors import QuoteNotFoundError, QuoteNotOwnedError, TooManyActiveQuotesError
from esim_mcp.purchase.models import (
    PaymentMethod,
    PurchaseQuote,
    QuotedBundle,
    QuotedPrice,
    QuotedWallet,
    QuoteStatus,
    SearchContext,
    expiry_from,
    utc_now,
)
from esim_mcp.purchase.store import PurchaseQuoteStore, QuoteOwner
from esim_mcp.purchase.validation import new_quote_id, require_usable_quote
from esim_mcp.settings import Settings

logger = logging.getLogger(__name__)


def quote_ref(quote_id: str) -> str:
    """Short, non-reversible reference safe to put in a log record."""
    return quote_id[:8]


class PurchaseQuoteService:
    """Create, read and cancel MCP-local purchase quotes.

    Nothing in this class talks to the eSIM backend, so no method here can create an order,
    move money, reach Stripe or provision an eSIM.
    """

    def __init__(self, settings: Settings, store: PurchaseQuoteStore) -> None:
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
        bundle: QuotedBundle,
        payment_method: PaymentMethod,
        price: QuotedPrice,
        wallet: QuotedWallet | None,
        search_context: SearchContext,
        locale: str,
        now: datetime | None = None,
    ) -> PurchaseQuote:
        """Store a new quote for ``owner``, superseding an equivalent active one.

        Raises :class:`TooManyActiveQuotesError` when the owner is already at the configured
        limit *after* superseding, so re-preparing an existing choice never trips it.
        """
        moment = now or utc_now()

        async with self._store.lock(owner):
            active = await self._store.list_active(owner, now=moment)

            superseded = [
                existing
                for existing in active
                if existing.equivalent_to(bundle_code=bundle.bundle_code, payment_method=payment_method)
            ]
            for existing in superseded:
                await self._store.set_status(owner, existing.quote_id, QuoteStatus.CANCELLED)
                logger.info("quote_superseded", extra={"quote_ref": quote_ref(existing.quote_id)})

            remaining = len(active) - len(superseded)
            if remaining >= self.max_active:
                logger.info("quote_limit_reached", extra={"active_quotes": remaining, "limit": self.max_active})
                raise TooManyActiveQuotesError(
                    f"This client already has {remaining} prepared quotes, which is the maximum. Ask the user "
                    "which one they no longer want and cancel it, or wait for one to expire, then prepare again."
                )

            quote = PurchaseQuote(
                quote_id=new_quote_id(),
                owner_session_key=owner.session_key,
                owner_user_ref=owner.user_ref,
                identity_source=identity_source,
                bundle=bundle,
                payment_method=payment_method,
                price=price,
                wallet=wallet,
                search_context=search_context,
                locale=locale,
                created_at=moment,
                expires_at=expiry_from(moment, self.ttl_seconds),
            )
            await self._store.save(quote)

        logger.info(
            "quote_prepared",
            extra={
                "quote_ref": quote_ref(quote.quote_id),
                "payment_method": payment_method.value,
                "superseded": len(superseded),
                # Never the amount, never the balance: a log line is not a receipt.
                "order_created": False,
                "charged": False,
            },
        )
        return quote

    async def get(
        self,
        owner: QuoteOwner,
        quote_id: str,
        *,
        now: datetime | None = None,
    ) -> PurchaseQuote:
        """Return one usable quote owned by ``owner``.

        An unknown id, and an id belonging to another owner, both raise
        :class:`QuoteNotFoundError` -- identical responses, so a caller cannot use this to
        learn that somebody else's quote exists.
        """
        quote = await self._require_owned(owner, quote_id)
        return require_usable_quote(quote, now=now)

    async def get_record(
        self,
        owner: QuoteOwner,
        quote_id: str,
    ) -> PurchaseQuote:
        """Return one owned quote **whatever its status**, for honest state reporting.

        Used where an expired or cancelled quote should be described rather than raised: an
        assistant asking about "the prepared purchase" needs to be able to say that it lapsed.
        """
        return await self._require_owned(owner, quote_id)

    async def cancel(self, owner: QuoteOwner, quote_id: str, *, now: datetime | None = None) -> PurchaseQuote:
        """Cancel one owned quote and return its final state.

        Cancelling is local and idempotent. An already-cancelled or expired quote is returned
        as-is rather than raising, because the user's intent ("get rid of it") is already
        satisfied and there is nothing in the backend to undo.
        """
        quote = await self._require_owned(owner, quote_id)
        moment = now or utc_now()
        if quote.effective_status(moment) is not QuoteStatus.ACTIVE:
            return quote
        updated = await self._store.set_status(owner, quote_id, QuoteStatus.CANCELLED)
        if updated is None:  # pragma: no cover - lost race with an invalidation
            raise QuoteNotFoundError()
        logger.info("quote_cancelled", extra={"quote_ref": quote_ref(quote_id), "order_cancelled": False})
        return updated

    async def consume(self, owner: QuoteOwner, quote_id: str) -> PurchaseQuote:
        """Mark one owned quote as spent, so it can never be turned into a second order.

        Called from exactly one place: immediately after the platform has confirmed a
        completed purchase. It is deliberately *not* called for a failed, ambiguous or
        unknown outcome -- consuming a quote whose purchase may not have happened would
        destroy the only record of what the user agreed to, and consuming one whose purchase
        certainly did not happen would cost them a re-preparation for nothing.
        """
        quote = await self._require_owned(owner, quote_id)
        updated = await self._store.set_status(owner, quote_id, QuoteStatus.CONSUMED)
        if updated is None:  # pragma: no cover - lost race with an invalidation
            raise QuoteNotFoundError()
        logger.info("quote_consumed", extra={"quote_ref": quote_ref(quote.quote_id)})
        return updated

    async def list_active(self, owner: QuoteOwner, *, now: datetime | None = None) -> list[PurchaseQuote]:
        """Every still-usable quote for ``owner``, newest first."""
        active = await self._store.list_active(owner, now=now)
        return sorted(active, key=lambda quote: quote.created_at, reverse=True)

    async def invalidate_session(self, session_key: str) -> int:
        """Cancel every quote held under one MCP session key (logout, session invalidation)."""
        cancelled = await self._store.invalidate_session(session_key)
        if cancelled:
            logger.info("quotes_invalidated_with_session", extra={"cancelled_quotes": cancelled})
        return cancelled

    # ------------------------------------------------------------------ internals

    async def _require_owned(self, owner: QuoteOwner, quote_id: str) -> PurchaseQuote:
        """Fetch an owned quote, re-checking ownership above the store as defence in depth."""
        quote = await self._store.get(owner, quote_id)
        if quote is None:
            raise QuoteNotFoundError()
        if not owner.matches(quote):
            # Unreachable through the shipped store, which scopes its own lookups. Kept so a
            # future store implementation cannot silently hand a caller somebody else's quote.
            logger.error("quote_ownership_violation", extra={"quote_ref": quote_ref(quote_id)})
            raise QuoteNotOwnedError()
        return quote
