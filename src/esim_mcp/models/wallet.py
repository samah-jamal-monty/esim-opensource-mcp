"""Wallet model mirroring the backend's ``UserWalletResponse``.

The backend contract is deliberately small::

    class UserWalletResponse(BaseModel):
        balance: float
        currency: str

``balance`` is the wallet's ``amount`` column already converted into the currency asked for
through ``X-Currency``, and the route answers ``data: null`` when the user has no wallet row
at all -- so "no wallet" is a normal, non-error backend response that this layer has to
represent rather than crash on.

Money is parsed straight into :class:`~decimal.Decimal` **via its string form**. The backend
serializes a float, and ``Decimal(8.06)`` is ``8.059999...``, so every comparison and every
subtraction in :mod:`esim_mcp.purchase` would inherit a binary rounding artifact. Going
through ``str`` keeps the two decimal places the backend actually sent.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from esim_mcp.errors import InvalidBackendResponseError

logger = logging.getLogger(__name__)


def decimal_from_number(value: Any) -> Decimal | None:
    """Best-effort :class:`Decimal` from a backend number, via ``str`` to avoid float noise.

    Returns ``None`` when the value is missing or not a finite number, so a caller can
    decide to fail rather than silently treat an unreadable amount as zero.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    try:
        candidate = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return candidate if candidate.is_finite() else None


class UserWallet(BaseModel):
    """``UserWalletResponse`` -- the signed-in user's own balance, in one currency."""

    model_config = ConfigDict(extra="ignore")

    balance: Decimal
    currency: str = ""

    @field_validator("balance", mode="before")
    @classmethod
    def _decimal_balance(cls, value: Any) -> Any:
        parsed = decimal_from_number(value)
        if parsed is None:
            raise ValueError("balance must be a finite number")
        return parsed

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return (value or "").strip().upper()


def parse_user_wallet(data: Any) -> UserWallet | None:
    """Validate a ``/wallet/user_wallet_by_user`` payload.

    ``None`` means the backend reported no wallet for this user -- a documented outcome,
    distinct from an unreadable payload, which raises.
    """
    if data is None:
        return None
    if not isinstance(data, dict):
        logger.warning("wallet_response_not_an_object")
        raise InvalidBackendResponseError()
    try:
        return UserWallet.model_validate(data)
    except ValueError:
        # The payload carries a balance; only the fact of the failure is logged.
        logger.warning("wallet_response_validation_failed")
        raise InvalidBackendResponseError() from None
