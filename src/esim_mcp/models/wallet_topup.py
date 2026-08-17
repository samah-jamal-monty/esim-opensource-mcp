"""The wallet top-up payloads the platform returns, parsed defensively.

Three payloads: what the platform will accept for a top-up, the answer to *open a hosted
page for this top-up*, and the answer to *what happened to it*. All three are parsed the way
:mod:`esim_mcp.models.card` parses its own -- by naming every field this server is willing
to read, with ``extra="ignore"`` -- and for the same reason: a hosted-checkout payload from
a payment provider is exactly the shape of thing that carries a session id, a client secret
or a publishable key, and none of those may reach a model's context. Because the field set
is closed, that property is structural rather than aspirational.

The one value the user is asked to act on -- the checkout link -- is validated rather than
trusted, by the same :func:`~esim_mcp.models.card.safe_checkout_url` the card flow uses.
There is deliberately only one implementation of that check in this codebase.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class WalletTopupStatus(StrEnum):
    """The lifecycle of one wallet top-up, as the platform reports it.

    Smaller than the card lifecycle because a top-up has no eSIM to provision: money
    arriving is the last thing that happens to it.

    * ``PENDING`` -- the page exists and nobody has paid yet;
    * ``PAID`` -- the platform's webhook recorded the payment and credited the balance;
    * ``FAILED`` -- the payment did not go through. Nothing was taken;
    * ``EXPIRED`` -- the page lapsed unpaid. Nothing was taken.
    """

    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        """True when no later check can change this answer."""
        return self in (WalletTopupStatus.PAID, WalletTopupStatus.FAILED, WalletTopupStatus.EXPIRED)

    @property
    def money_arrived(self) -> bool:
        """True only where the platform states the payment was received.

        Never inferred from a redirect, a returning user, or the existence of a page.
        """
        return self is WalletTopupStatus.PAID


#: The platform's own normalized status words, and **only** those. Anything unrecognized
#: becomes ``None`` and is reported as "could not be read" rather than guessed at: an unknown
#: word must never degrade to ``PENDING`` (which reads as "keep waiting") or to ``FAILED``
#: (which reads as "nothing was taken"), because both are claims with no evidence behind them.
_NORMALIZED_STATUSES: dict[str, WalletTopupStatus] = {status.value: status for status in WalletTopupStatus}


def parse_topup_status(value: str | None) -> WalletTopupStatus | None:
    """Map one platform status word onto a typed status, or ``None`` when unrecognized."""
    if not value:
        return None
    return _NORMALIZED_STATUSES.get(value.strip().upper())


class BackendTopupLimit(BaseModel):
    """One limit or fee the platform states. ``McpWalletTopupLimit``, field for field."""

    model_config = ConfigDict(extra="ignore")

    code: str | None = None
    value: float | int | str | None = None
    remaining: float | int | str | None = None
    unit: str | None = None


class BackendTopupOptions(BaseModel):
    """What the platform will accept for a top-up right now, for this caller.

    Every number here is the platform's: the minimum is the rule its write path enforces and
    the ceilings are computed from the caller's own history. Nothing in this server derives,
    adjusts or supplements any of them.
    """

    model_config = ConfigDict(extra="ignore")

    currency: str | None = None
    minimum_amount_exclusive: str | float | int | None = None
    maximum_amount: str | float | int | None = None
    current_balance: str | float | int | None = None
    limits: list[BackendTopupLimit] = []
    can_topup: bool = True
    fees: list[BackendTopupLimit] = []
    message: str | None = None


class BackendWalletTopupCheckout(BaseModel):
    """The platform's answer to "open a hosted page for this top-up".

    ``idempotent_replay`` is the platform's own statement that it resolved this request onto
    a top-up the caller had already started and not yet paid -- so the link is the one they
    already have, not a second one to pay.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    payment_reference: str | None = None
    order_id: str | None = None
    checkout_url: str | None = None
    status: str | None = None
    amount: str | float | int | None = None
    currency: str | None = None
    expires_at: str | None = None
    next_action: str | None = None
    idempotent_replay: bool = False
    #: Named so it is known to be *dropped*, never forwarded. A correlation id is a backend
    #: tracing handle: useful in the platform's logs, identifier-shaped in a conversation.
    correlation_id: str | None = None
    #: Backend-supplied prose. Internal classification only; never forwarded to a client.
    message: str | None = None

    @property
    def topup_status(self) -> WalletTopupStatus | None:
        return parse_topup_status(self.status)


class BackendWalletTopupStatus(BaseModel):
    """The platform's answer to "what happened to this top-up".

    ``paid`` is the platform's own statement, written by its signature-verified webhook. It
    is never set here, never inferred from a redirect and never taken from a caller.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    payment_reference: str | None = None
    status: str | None = None
    order_id: str | None = None
    amount: str | float | int | None = None
    currency: str | None = None
    expires_at: str | None = None
    paid: bool | None = None
    next_action: str | None = None
    correlation_id: str | None = None
    message: str | None = None

    @property
    def topup_status(self) -> WalletTopupStatus | None:
        return parse_topup_status(self.status)


def parse_topup_options(data: Any) -> BackendTopupOptions | None:
    """Parse a top-up options payload, or return ``None`` when it cannot be read."""
    return _parse(BackendTopupOptions, data, "wallet_topup_options_unreadable")


def parse_wallet_topup_checkout(data: Any) -> BackendWalletTopupCheckout | None:
    """Parse a top-up checkout payload, or return ``None`` when it cannot be read."""
    return _parse(BackendWalletTopupCheckout, data, "wallet_topup_checkout_unreadable")


def parse_wallet_topup_status(data: Any) -> BackendWalletTopupStatus | None:
    """Parse a top-up status payload, or return ``None`` when it cannot be read."""
    return _parse(BackendWalletTopupStatus, data, "wallet_topup_status_unreadable")


def _parse[ModelT: BaseModel](model: type[ModelT], data: Any, log_event: str) -> ModelT | None:
    if not isinstance(data, dict):
        return None
    try:
        return model.model_validate(data)
    except Exception:  # pragma: no cover - defensive; every field is already optional
        logger.warning(log_event)
        return None
