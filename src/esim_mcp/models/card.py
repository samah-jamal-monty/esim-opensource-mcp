"""The card-checkout payloads the platform returns, parsed defensively.

Two payloads live here: the answer to *open a hosted payment page for this quote*, and the
answer to *what happened to that payment*. Both are parsed the same way the wallet purchase
result is -- by naming every field this server is willing to read, with ``extra="ignore"`` --
and for the same reason: a field somebody adds upstream must not be able to appear in an MCP
result simply because it exists.

That matters more here than anywhere else in this codebase. A hosted-checkout payload from a
payment provider is exactly the shape of thing that carries a provider session id, a client
secret, a publishable key or a raw payment-intent reference, and none of those may ever reach
a model's context. Because the field set is closed, the safety property is structural: a
secret cannot leak by being forwarded, only by somebody adding a field *and* naming it here.

The one value the user is asked to act on -- the checkout link -- is validated rather than
trusted (:func:`safe_checkout_url`). Everything else is descriptive text.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

#: Longest checkout link this server will pass on. A hosted payment URL is a hundred-odd
#: characters; anything near this bound is not one.
MAX_CHECKOUT_URL_LENGTH = 2048


class CardPaymentStatus(StrEnum):
    """The lifecycle of one card payment, as the platform reports it.

    The four groups behave differently and must never be collapsed:

    * ``PENDING`` -- the page exists and nobody has paid yet;
    * ``PAID`` / ``PROVISIONING`` -- money arrived, the eSIM is not ready yet;
    * ``COMPLETED`` -- money arrived *and* the order is done;
    * ``FAILED`` / ``EXPIRED`` / ``CANCELLED`` -- nothing was taken, and a new page is needed;
    * ``AMBIGUOUS`` -- the platform itself cannot say. Stop, and escalate.
    """

    PENDING = "PENDING"
    PAID = "PAID"
    PROVISIONING = "PROVISIONING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    AMBIGUOUS = "AMBIGUOUS"

    @property
    def is_terminal(self) -> bool:
        """True when no later check can change this answer."""
        return self in (
            CardPaymentStatus.COMPLETED,
            CardPaymentStatus.FAILED,
            CardPaymentStatus.EXPIRED,
            CardPaymentStatus.CANCELLED,
            CardPaymentStatus.AMBIGUOUS,
        )

    @property
    def money_arrived(self) -> bool:
        """True only where the platform states the payment was received.

        Never inferred from a redirect, a returning user, or the mere existence of a page.
        """
        return self in (CardPaymentStatus.PAID, CardPaymentStatus.PROVISIONING, CardPaymentStatus.COMPLETED)


#: The platform's own normalized status words, and **only** those. The backend normalizes
#: before answering, so no provider spelling ("succeeded", "canceled", "requires_payment")
#: reaches this layer -- and inventing a mapping for one would mean guessing at a word this
#: server has no evidence about.
#:
#: Anything unrecognized becomes ``None`` and is reported as "could not be read" rather than
#: guessed at: an unknown word must never degrade to ``PENDING`` (which reads as "keep
#: waiting") or to ``FAILED`` (which reads as "nothing was taken"), because both are claims
#: this server would have no evidence for.
_NORMALIZED_STATUSES: dict[str, CardPaymentStatus] = {status.value: status for status in CardPaymentStatus}


def parse_status(value: str | None) -> CardPaymentStatus | None:
    """Map one platform status word onto a typed status, or ``None`` when unrecognized.

    Only case and surrounding whitespace are normalized. No synonym is accepted.
    """
    if not value:
        return None
    return _NORMALIZED_STATUSES.get(value.strip().upper())


class BackendCardCheckout(BaseModel):
    """The platform's answer to "open a hosted payment page for this quote".

    The field set is exactly the backend's documented create-checkout response, with no
    invented aliases: a name this server reads has to be a name the platform actually sends.
    ``extra="ignore"`` closes the rest, so a provider session id, a client secret or a
    publishable key appearing upstream cannot reach a result by being forwarded.

    Every field is optional. A payload missing something is a fact to report honestly, never
    a parse failure -- except the link and the reference, whose absence the caller treats as
    "no usable page was created", because without either one there is nothing to give the
    user and nothing to check later.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    payment_reference: str | None = None
    order_id: str | None = None
    checkout_url: str | None = None
    status: str | None = None
    amount: str | float | int | None = None
    currency: str | None = None
    expires_at: str | None = None
    idempotent_replay: bool = False
    #: Named so it is known to be *dropped*, never forwarded. A correlation id is a backend
    #: tracing handle: useful in a log line on the platform's side, meaningless and
    #: identifier-shaped in a conversation.
    correlation_id: str | None = None
    #: Backend-supplied prose. Internal classification only; never forwarded to a client.
    message: str | None = None

    @property
    def payment_status(self) -> CardPaymentStatus | None:
        return parse_status(self.status)


class BackendCardPaymentStatus(BaseModel):
    """The platform's answer to "what happened to this card payment".

    Same rule as above: exactly the documented fields, no invented aliases, everything else
    dropped at parse time.

    ``provisioned`` is the platform's own statement about the eSIM, and it is deliberately
    separate from ``status``: money arriving and an eSIM existing are different facts, and a
    ``PAID`` payment whose eSIM is not ready yet must never be described as ready.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    payment_reference: str | None = None
    status: str | None = None
    order_id: str | None = None
    amount: str | float | int | None = None
    currency: str | None = None
    bundle_code: str | None = None
    quote_reference: str | None = None
    expires_at: str | None = None
    provisioned: bool | None = None
    next_action: str | None = None
    #: Named so it is known to be dropped, exactly as on the checkout payload above.
    correlation_id: str | None = None
    #: Backend-supplied prose. Internal classification only; never forwarded to a client.
    message: str | None = None

    @property
    def payment_status(self) -> CardPaymentStatus | None:
        return parse_status(self.status)


def parse_card_checkout(data: Any) -> BackendCardCheckout | None:
    """Parse a checkout payload, or return ``None`` when it cannot be read."""
    return _parse(BackendCardCheckout, data, "card_checkout_payload_unreadable")


def parse_card_payment_status(data: Any) -> BackendCardPaymentStatus | None:
    """Parse a payment-status payload, or return ``None`` when it cannot be read."""
    return _parse(BackendCardPaymentStatus, data, "card_payment_status_payload_unreadable")


def _parse[ModelT: BaseModel](model: type[ModelT], data: Any, log_event: str) -> ModelT | None:
    if not isinstance(data, dict):
        return None
    try:
        return model.model_validate(data)
    except Exception:  # pragma: no cover - defensive; every field is already optional
        logger.warning(log_event)
        return None


def safe_checkout_url(value: str | None) -> str | None:
    """Return ``value`` only if it is a plain ``https`` link this server will show a user.

    Refused rather than sanitized, because a link is acted on rather than read: an address
    that is not what a hosted payment page looks like is a bug or an attack, and repairing
    one would be worse than declining it. Specifically rejected are non-``https`` schemes
    (``javascript:``, ``data:``, plain ``http``), embedded credentials, missing hosts,
    whitespace, control characters and anything absurdly long.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_CHECKOUT_URL_LENGTH:
        return None
    if any(character.isspace() or ord(character) < 0x20 for character in candidate):
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return candidate
