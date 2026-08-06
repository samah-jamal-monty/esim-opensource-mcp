"""The two account-history payloads this server reads, parsed defensively.

Both mirror an existing backend contract exactly, with no invented aliases and no invented
fields:

* :class:`BackendEsim` is ``EsimBundleResponse`` from ``GET /user/my-esim``;
* :class:`BackendOrderHistoryEntry` is ``UserOrderHistoryResponse`` from
  ``GET /user/order-history``.

Same rule as :mod:`esim_mcp.models.card`: every field this server is willing to read is
named, and ``extra="ignore"`` closes the rest. That is what makes the safety property
structural rather than aspirational -- a field added upstream cannot reach a model's context
by being forwarded, only by somebody adding it *and* naming it here.

Two things the backend sends that are named below purely so they are known to be **dropped**:

* ``payment_details.receipt_email`` and ``payment_details.address`` -- a complete email
  address and a billing address. This server masks contact details everywhere else; letting
  them through on an order receipt would undo that in one line.
* ``payment_details.id`` -- the payment provider's own charge identifier. Identifier-shaped,
  meaningless in a conversation, and exactly the kind of value that must never be quoted at
  a user.

What is *not* here, because the backend does not send it
--------------------------------------------------------
**Data consumption.** ``EsimBundleResponse`` carries no used/remaining figure. Consumption
lives behind ``GET /user/consumption/{iccid}``, which
:data:`~esim_mcp.client.base.FORBIDDEN_PATH_MARKERS` refuses outright. So no consumption
field is modelled, and none is guessed at from validity dates.

Dates are epoch seconds, not ISO
--------------------------------
``payment_date``, ``order_date`` and ``transaction_history[].created_at`` are passed through
``int(datetime.fromisoformat(value).timestamp())`` by the backend's own field validators, so
they arrive as *stringified unix timestamps* ("1754472554"). They are kept verbatim under
their backend names, and :func:`epoch_to_iso` renders the same instant in a form an assistant
can read to a user without inventing one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


def epoch_to_iso(value: Any) -> str | None:
    """Render a backend epoch-seconds string as an ISO-8601 UTC instant, or ``None``.

    Never raises and never guesses: a value that is not a plain epoch is reported as
    unreadable rather than turned into a date that was not sent.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


class BackendPaymentDetails(BaseModel):
    """``PaymentDetailsDTO`` -- the safe half of it.

    ``card_display`` is already masked by the backend to ``"<brand> ****<last4>"``, which is
    the whole of what a user needs to recognize which card they used. The raw ``card_number``
    (Stripe's last4), the charge ``id``, ``receipt_email``, ``address``, ``country`` and
    ``description`` are deliberately unnamed and therefore dropped at parse time.
    """

    model_config = ConfigDict(extra="ignore")

    payment_method: str | None = None
    display_brand: str | None = None
    card_display: str | None = None


class BackendTransaction(BaseModel):
    """``TransactionHistoryResponse`` -- one bundle event on one eSIM.

    This is the platform's record of a plan being added to a SIM, not a payment ledger and
    not a consumption reading. ``bundle`` is dropped: the plan it describes is already on the
    parent eSIM, and repeating a whole ``BundleDTO`` per event would bury the result.
    """

    model_config = ConfigDict(extra="ignore")

    bundle_type: str | None = None
    plan_started: bool | None = None
    bundle_expired: bool | None = None
    created_at: str | None = None


class BackendEsim(BaseModel):
    """``EsimBundleResponse`` -- one eSIM the signed-in user owns.

    ``qr_code_value``, ``activation_code`` and ``smdp_address`` are install credentials: an
    eSIM profile installs once, so whoever presents them first claims the SIM. They are read
    because retrieving them is the entire point of the tool, and they are never logged.
    """

    model_config = ConfigDict(extra="ignore")

    iccid: str | None = None
    label_name: str | None = None
    order_number: str | None = None
    order_status: str | None = None
    plan_started: bool | None = None
    bundle_expired: bool | None = None
    is_topup_allowed: bool | None = None
    shared_with: int | None = None

    # Install information.
    qr_code_value: str | None = None
    activation_code: str | None = None
    smdp_address: str | None = None

    # Dates, as epoch-seconds strings. See the module docstring.
    validity_date: str | None = None
    payment_date: str | None = None

    # The plan on the SIM.
    bundle_code: str | None = None
    bundle_name: str | None = None
    bundle_marketing_name: str | None = None
    display_title: str | None = None
    display_subtitle: str | None = None
    gprs_limit_display: str | None = None
    unlimited: bool | None = None
    validity_display: str | None = None
    validity_label: str | None = None
    plan_type: str | None = None
    activity_policy: str | None = None
    count_countries: int | None = None
    currency_code: str | None = None
    price_display: str | None = None

    transaction_history: list[BackendTransaction] = []


class BackendOrderHistoryEntry(BaseModel):
    """``UserOrderHistoryResponse`` -- one order the signed-in user placed.

    ``company_*`` are the reseller's own invoice details, identical on every row; they say
    nothing about the order and are dropped. ``bundle_details`` is a full ``BundleDTO``, of
    which only the plan facts a user would recognize are read.
    """

    model_config = ConfigDict(extra="ignore")

    order_number: str | None = None
    order_status: str | None = None
    order_type: str | None = None
    order_amount: float | int | str | None = None
    order_currency: str | None = None
    order_display_price: str | None = None
    order_date: str | None = None
    quantity: int | None = None
    payment_type: str | None = None
    payment_details: BackendPaymentDetails | None = None
    bundle_details: BackendEsimBundleSummary | None = None


class BackendEsimBundleSummary(BaseModel):
    """The plan facts read out of an order's ``bundle_details`` (``BundleDTO``).

    Deliberately a narrow slice. A ``BundleDTO`` carries stock flags, internal info codes and
    a full country list; none of that belongs in a list of past orders.
    """

    model_config = ConfigDict(extra="ignore")

    bundle_code: str | None = None
    bundle_name: str | None = None
    bundle_marketing_name: str | None = None
    display_title: str | None = None
    display_subtitle: str | None = None
    gprs_limit_display: str | None = None
    unlimited: bool | None = None
    validity_display: str | None = None
    plan_type: str | None = None
    count_countries: int | None = None
    price_display: str | None = None
    currency_code: str | None = None


BackendOrderHistoryEntry.model_rebuild()


def parse_esims(data: Any) -> list[BackendEsim]:
    """Parse the eSIM list, dropping rows that cannot be read rather than failing all of them.

    An account with no eSIMs is a normal answer, not an error: the backend replies with a
    successful envelope carrying ``[]`` or ``null``, and both mean the same thing here.
    """
    return _parse_list(BackendEsim, data, "esim_row_unreadable")


def parse_order_history(data: Any) -> list[BackendOrderHistoryEntry]:
    """Parse the order-history list, dropping rows that cannot be read."""
    return _parse_list(BackendOrderHistoryEntry, data, "order_history_row_unreadable")


def _parse_list[ModelT: BaseModel](model: type[ModelT], data: Any, log_event: str) -> list[ModelT]:
    if data is None:
        return []
    if not isinstance(data, list):
        logger.warning("%s_not_a_list", log_event)
        return []
    parsed: list[ModelT] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            parsed.append(model.model_validate(row))
        except Exception:  # pragma: no cover - defensive; every field is optional
            logger.warning(log_event)
    return parsed
