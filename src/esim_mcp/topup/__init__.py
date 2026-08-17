"""MCP-local bookkeeping for the two top-up flows: an eSIM top-up, and a wallet top-up.

Nothing in this package performs I/O. Like :mod:`esim_mcp.purchase` it cannot reach the
network, so no module here can create an order, move money or credit a balance. What it owns
is *what the user agreed to*: a quote records the plan or the amount the platform priced a
moment ago, so the tool that acts on it acts on the platform's numbers rather than on
anything a model produced.

Three things this package is careful **not** to be:

* it is not an idempotency mechanism for the wallet top-up. Duplicate protection there is
  the platform's own pending ``user_order`` row, which is durable; the record kept here only
  replays a link this client has already been handed, and losing it costs a repeated call
  that the platform then answers with the same page;
* it is not idempotency for the **QA eSIM top-up** either, and that one matters more. The
  legacy route has no key to present, so :class:`EsimTopupExecution` does not hold one. What
  it holds is the fact that an attempt was made, which lets this server refuse a second one
  -- a process-local lock, not a platform guarantee, and the reason that capability is
  QA-only;
* it is not a hold. A quote reserves nothing, prices nothing and obliges the platform to
  nothing.
"""

from esim_mcp.topup.models import (
    EsimTarget,
    EsimTopupExecution,
    EsimTopupExecutionStatus,
    EsimTopupQuote,
    TopupQuoteStatus,
    TopupStatus,
    WalletTopupCheckout,
    WalletTopupQuote,
)
from esim_mcp.topup.service import (
    EsimTopupExecutionService,
    EsimTopupQuoteService,
    WalletTopupCheckoutService,
    WalletTopupQuoteService,
)
from esim_mcp.topup.store import (
    EsimTopupExecutionStore,
    EsimTopupQuoteStore,
    InMemoryEsimTopupExecutionStore,
    InMemoryEsimTopupQuoteStore,
    InMemoryWalletTopupCheckoutStore,
    InMemoryWalletTopupQuoteStore,
    WalletTopupCheckoutStore,
    WalletTopupQuoteStore,
)

__all__ = [
    "EsimTarget",
    "EsimTopupExecution",
    "EsimTopupExecutionService",
    "EsimTopupExecutionStatus",
    "EsimTopupExecutionStore",
    "EsimTopupQuote",
    "EsimTopupQuoteService",
    "EsimTopupQuoteStore",
    "InMemoryEsimTopupExecutionStore",
    "InMemoryEsimTopupQuoteStore",
    "InMemoryWalletTopupCheckoutStore",
    "InMemoryWalletTopupQuoteStore",
    "TopupQuoteStatus",
    "TopupStatus",
    "WalletTopupCheckout",
    "WalletTopupCheckoutService",
    "WalletTopupCheckoutStore",
    "WalletTopupQuote",
    "WalletTopupQuoteService",
    "WalletTopupQuoteStore",
]
