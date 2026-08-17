"""The purchase result the platform returns, parsed defensively.

The platform's own purchase payload deliberately excludes tokens, the raw idempotency key,
wallet transaction internals, provider credentials, the activation code and the ICCID. This
model keeps that promise on this side of the wire by naming every field it is willing to
read: ``extra="ignore"`` means a future backend field cannot appear in an MCP result simply
because somebody added it upstream.

Every field is optional. A purchase answer that is missing something is a *fact this server
has to report honestly* -- never a parse failure, and never a reason to guess. In particular
an unreadable payload after a possible charge must surface as "outcome unknown", which is
what :func:`parse_purchase_result` returning ``None`` lets the caller do.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

#: The one business status that means the plan was bought and the eSIM is ready.
COMPLETED = "COMPLETED"

#: Order status the platform reports for a purchase that went through.
ORDER_SUCCESS = "SUCCESS"


class BackendPurchaseResult(BaseModel):
    """One completed, failed or ambiguous purchase, as the platform described it."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: str | None = None
    order_id: str | None = None
    payment_method: str | None = None
    payment_status: str | None = None
    order_status: str | None = None
    idempotent_replay: bool = False
    provisioning_status: str | None = None
    next_action: str | None = None
    quote_reference: str | None = None
    #: Backend-supplied prose. Used for internal classification only; a platform message is
    #: never forwarded verbatim to an MCP client, because it is not written for an assistant
    #: to read out and may name internal state.
    message: str | None = Field(default=None)

    @property
    def is_completed(self) -> bool:
        """True only when the platform reports the full business outcome as complete."""
        return (self.status or "").strip().upper() == COMPLETED

    @property
    def is_order_successful(self) -> bool:
        return (self.order_status or "").strip().upper() == ORDER_SUCCESS


def parse_purchase_result(data: Any) -> BackendPurchaseResult | None:
    """Parse a purchase payload, or return ``None`` when it cannot be read.

    ``None`` is a meaningful answer here and must not be turned into an empty result: after a
    request that may have debited a wallet, "the platform said something I could not read" is
    an unknown outcome, not a failed one.
    """
    if not isinstance(data, dict):
        return None
    try:
        return BackendPurchaseResult.model_validate(data)
    except Exception:  # pragma: no cover - defensive; every field is already optional
        logger.warning("purchase_result_unreadable")
        return None


class BackendLegacyTopupResult(BaseModel):
    """The platform's ``PaymentIntentResponse``, as its *legacy* top-up route returns it.

    A different contract from :class:`BackendPurchaseResult` and parsed separately on
    purpose: the legacy route reports the business outcome in ``payment_status`` and has no
    ``status`` field at all, so reading it with the MCP purchase model would silently see
    every top-up as incomplete.

    **The closed field set matters more here than anywhere else in this module.** A
    ``PaymentIntentResponse`` can carry ``publishable_key``,
    ``payment_intent_client_secret``, ``customer_id`` and ``customer_ephemeral_key_secret``.
    None of them is named below, so none of them survives parsing and none can reach a
    model's context by being forwarded. That is structural, not a promise: adding one would
    take somebody naming it here.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    order_id: str | None = None
    payment_status: str | None = None

    @property
    def is_completed(self) -> bool:
        """True only where the platform states the wallet payment completed.

        Anything else -- ``PENDING``, ``PENDING_VERIFICATION``, a blank, a word this server
        does not know -- is **not** a completion. After a request that may have debited a
        wallet, that is reported as unknown rather than as a failure.
        """
        return (self.payment_status or "").strip().upper() == COMPLETED


def parse_legacy_topup_result(data: Any) -> BackendLegacyTopupResult | None:
    """Parse a legacy top-up payload, or return ``None`` when it cannot be read.

    ``None`` is meaningful and must not become an empty result: after a call that may have
    debited a wallet, "the platform said something I could not read" is an unknown outcome.
    """
    if not isinstance(data, dict):
        return None
    try:
        return BackendLegacyTopupResult.model_validate(data)
    except Exception:  # pragma: no cover - defensive; every field is already optional
        logger.warning("legacy_topup_result_unreadable")
        return None
