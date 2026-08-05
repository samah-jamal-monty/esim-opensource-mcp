"""Quote-lifecycle validation, identifier generation and wallet arithmetic.

Three jobs, all deliberately free of I/O so they are directly unit-testable:

* **Identifiers** -- :func:`new_quote_id` is the only place a quote id is produced.
* **Lifecycle** -- :func:`require_usable_quote` is the single gate that decides whether a
  stored quote may still be acted on, so ``get`` and ``cancel`` cannot drift apart.
* **Money** -- every comparison and subtraction between a price and a balance happens here,
  in :class:`~decimal.Decimal`. No float arithmetic touches money anywhere in this package.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal

from esim_mcp.errors import (
    InsufficientWalletBalanceError,
    InvalidInputError,
    QuoteCancelledError,
    QuoteConsumedError,
    QuoteExpiredError,
    UnsupportedPaymentMethodError,
)
from esim_mcp.purchase.models import PaymentMethod, PurchaseQuote, QuotedWallet, QuoteStatus

#: Bytes of entropy behind a quote id. 32 bytes is ~43 url-safe characters: not enumerable,
#: and not derived from anything about the user or the plan.
_QUOTE_ID_BYTES = 32


def new_quote_id() -> str:
    """A cryptographically random, opaque quote id.

    Uses :mod:`secrets`, never a counter, a timestamp or a UUID whose version encodes
    predictable input. The value deliberately carries **no** email, phone, user id, session
    key, bundle code or device id, so it can be shown to the model and echoed back without
    disclosing anything -- and it cannot be guessed from a quote the caller already has.
    """
    return secrets.token_urlsafe(_QUOTE_ID_BYTES)


def parse_payment_method(value: str | None) -> PaymentMethod:
    """Validate a caller-supplied payment method.

    Accepts only ``Wallet`` and ``Card``, case-insensitively. ``DCB`` -- which the backend
    itself supports -- is rejected here on purpose, with the same message as any other
    unsupported value, so the model asks the user instead of guessing.
    """
    candidate = (value or "").strip()
    if not candidate:
        raise UnsupportedPaymentMethodError(
            "No payment method was chosen. Ask the user whether they want to use their wallet balance or a "
            "card, then prepare the quote again. Never choose for them."
        )
    for method in PaymentMethod:
        if candidate.casefold() == method.value.casefold():
            return method
    raise UnsupportedPaymentMethodError()


def require_usable_quote(quote: PurchaseQuote, *, now: datetime | None = None) -> PurchaseQuote:
    """Return the quote if it may still be acted on, else raise the matching typed error.

    Expiry is evaluated from the clock rather than from the stored status, so a quote whose
    TTL lapsed without a sweep is reported as expired instead of active.
    """
    status = quote.effective_status(now)
    if status is QuoteStatus.EXPIRED:
        raise QuoteExpiredError()
    if status is QuoteStatus.CANCELLED:
        raise QuoteCancelledError()
    if status is QuoteStatus.CONSUMED:
        raise QuoteConsumedError()
    return quote


def require_positive_price(amount: Decimal | None, currency: str) -> Decimal:
    """Reject a plan whose displayed price could not be read from the backend.

    A missing price is a refusal rather than a zero: quoting "free" for a plan whose amount
    the platform did not report would be the single most damaging thing this phase could say.
    A zero the backend *did* report is passed through -- that is the platform's own value to
    explain, not this server's to hide.
    """
    if amount is None or not currency:
        raise InvalidInputError(
            "The platform did not report a usable price for that plan, so it cannot be quoted. Tell the user "
            "the price is unavailable and offer another plan."
        )
    if amount < 0:
        raise InvalidInputError(
            "The platform reported a negative price for that plan, so it cannot be quoted. Tell the user the "
            "price is unavailable and offer another plan."
        )
    return amount


def evaluate_wallet(*, balance: Decimal, currency: str, price: Decimal) -> QuotedWallet:
    """Compare a balance with a price in :class:`Decimal` and describe the outcome.

    Returns a snapshot either way: an insufficient balance is a *fact about the quote*, not a
    failure to produce one, so the assistant can tell the user the shortfall and offer card
    payment instead. ``estimated_remaining_balance`` is only populated when the balance
    actually covers the price -- a negative "remaining balance" would be a meaningless number
    to read out.
    """
    sufficient = balance >= price
    return QuotedWallet(
        balance=balance,
        currency=currency,
        sufficient=sufficient,
        estimated_remaining_balance=(balance - price) if sufficient else None,
        shortfall=None if sufficient else (price - balance),
    )


def require_wallet_sufficient(quote: PurchaseQuote) -> PurchaseQuote:
    """Gate that the *execution* phase must pass before spending a wallet balance.

    Phase 3 deliberately does not call this: ``prepare_purchase`` returns a usable quote with
    ``sufficient=False`` so the assistant can explain the shortfall. It lives here, tested,
    because the check belongs with the rest of the money arithmetic rather than being written
    fresh inside a future payment path -- and because a wallet quote must never be executed
    without it.
    """
    if quote.payment_method is not PaymentMethod.WALLET:
        return quote
    if quote.wallet is None or not quote.wallet.sufficient:
        raise InsufficientWalletBalanceError()
    return quote
