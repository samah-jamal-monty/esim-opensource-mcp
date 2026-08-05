"""Purchase domain logic: quotes, and the bookkeeping that makes buying one safe.

Two layers, both free of I/O. The quote layer records what a user picked and what the
platform said about it. The execution layer decides whether a quote's purchase may be sent,
mints exactly one idempotency key per quote, and remembers the answer so a repeated
confirmation replays it instead of buying the plan twice.

**Nothing in this package performs backend I/O.** The request that can create an order and
debit a wallet is made by :mod:`esim_mcp.client.purchase` and orchestrated by
:mod:`esim_mcp.tools.purchase_execution`; what lives here can only ever say yes or no.
"""

from esim_mcp.purchase.execution import (
    MAX_EXECUTION_ATTEMPTS,
    ExecutionStatus,
    InMemoryPurchaseExecutionStore,
    PurchaseExecution,
    PurchaseExecutionService,
    PurchaseExecutionStore,
    key_fingerprint,
    new_idempotency_key,
    replay_error,
    require_attempts_remaining,
    require_confirmable_quote,
)
from esim_mcp.purchase.models import (
    QUOTE_SCHEMA_VERSION,
    PaymentMethod,
    PurchaseQuote,
    QuotedBundle,
    QuotedPrice,
    QuotedWallet,
    QuoteStatus,
    SearchContext,
    SearchContextKind,
    money_text,
)
from esim_mcp.purchase.service import PurchaseQuoteService, quote_ref
from esim_mcp.purchase.store import InMemoryPurchaseQuoteStore, PurchaseQuoteStore, QuoteOwner
from esim_mcp.purchase.validation import (
    evaluate_wallet,
    new_quote_id,
    parse_payment_method,
    require_positive_price,
    require_usable_quote,
    require_wallet_sufficient,
)

__all__ = [
    "MAX_EXECUTION_ATTEMPTS",
    "QUOTE_SCHEMA_VERSION",
    "ExecutionStatus",
    "InMemoryPurchaseExecutionStore",
    "InMemoryPurchaseQuoteStore",
    "PaymentMethod",
    "PurchaseExecution",
    "PurchaseExecutionService",
    "PurchaseExecutionStore",
    "PurchaseQuote",
    "PurchaseQuoteService",
    "PurchaseQuoteStore",
    "QuoteOwner",
    "QuoteStatus",
    "QuotedBundle",
    "QuotedPrice",
    "QuotedWallet",
    "SearchContext",
    "SearchContextKind",
    "evaluate_wallet",
    "key_fingerprint",
    "money_text",
    "new_idempotency_key",
    "new_quote_id",
    "parse_payment_method",
    "quote_ref",
    "replay_error",
    "require_attempts_remaining",
    "require_confirmable_quote",
    "require_positive_price",
    "require_usable_quote",
    "require_wallet_sufficient",
]
