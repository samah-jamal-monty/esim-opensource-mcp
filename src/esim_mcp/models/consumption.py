"""The live consumption payload, parsed defensively.

Mirrors the backend's ``ConsumptionResponse`` from ``GET /user/consumption/{iccid}`` exactly,
with no invented aliases and no invented fields::

    class ConsumptionResponse(BaseModel):
        data_allocated: float
        data_used: float
        data_remaining: float
        data_allocated_display: str
        data_used_display: str
        data_remaining_display: str
        plan_status: str
        expiry_date: str

The backend builds every one of those from the eSIM hub's live reading (``dataAllocated``,
``dataUsed``, ``dataRemaining``, ``dataUnit``, ``planStatus``, ``profileExpiryDate``), so
this is the platform's own live usage and not a projection of a catalogue allowance.

Same rule as :mod:`esim_mcp.models.card`: every field this server will read is named and
``extra="ignore"`` closes the rest.

What this module deliberately will not do
-----------------------------------------
**It never computes a usage percentage from a bundle, a price or a date.** The percentage
below is derived from ``data_allocated`` and ``data_used`` -- the two live figures the
platform just sent -- and it is ``None`` whenever either of them is missing or the allowance
is not a positive number. An unlimited or unmetered plan therefore reports *no* percentage
rather than a made-up one, which is the whole point: "we do not know" and "0%" are different
statements and only one of them is true.

Every field is optional here even though the backend declares them required, because a
missing figure is a fact to report honestly rather than a parse failure. A payload with
nothing readable in it at all is reported as "no usage data yet", never as zero usage.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from esim_mcp.models.wallet import decimal_from_number

logger = logging.getLogger(__name__)

#: Values the backend emits when the upstream reading has no usable value. Mirrors the
#: catalogue's own placeholder set so an "Unknown" never reaches a user as a real status.
_PLACEHOLDERS = frozenset({"", "-", "n/a", "na", "none", "null", "unknown"})


def clean_text(value: str | None) -> str | None:
    """Normalize whitespace and map the backend's placeholders onto ``None``."""
    if value is None:
        return None
    candidate = " ".join(str(value).split())
    return None if candidate.lower() in _PLACEHOLDERS else candidate


class BackendConsumption(BaseModel):
    """``ConsumptionResponse`` -- one eSIM's live usage, as the platform reported it."""

    model_config = ConfigDict(extra="ignore")

    data_allocated: float | int | str | None = None
    data_used: float | int | str | None = None
    data_remaining: float | int | str | None = None
    data_allocated_display: str | None = None
    data_used_display: str | None = None
    data_remaining_display: str | None = None
    plan_status: str | None = None
    expiry_date: str | None = None

    # ------------------------------------------------------------------ derived views

    @property
    def allocated(self) -> Decimal | None:
        return decimal_from_number(self.data_allocated)

    @property
    def used(self) -> Decimal | None:
        return decimal_from_number(self.data_used)

    @property
    def remaining(self) -> Decimal | None:
        return decimal_from_number(self.data_remaining)

    @property
    def status(self) -> str | None:
        return clean_text(self.plan_status)

    @property
    def expiry(self) -> str | None:
        return clean_text(self.expiry_date)

    @property
    def allocated_text(self) -> str | None:
        return clean_text(self.data_allocated_display)

    @property
    def used_text(self) -> str | None:
        return clean_text(self.data_used_display)

    @property
    def remaining_text(self) -> str | None:
        return clean_text(self.data_remaining_display)

    @property
    def usage_percent(self) -> float | None:
        """Used as a percentage of allocated, or ``None`` when it cannot be known.

        Derived from the two live figures and from nothing else. ``None`` when either is
        missing, when the allowance is not positive (an unlimited or unmetered plan), or
        when the reading is not finite -- because a percentage nobody can compute must be
        absent from the result rather than defaulted to zero.
        """
        allocated, used = self.allocated, self.used
        if allocated is None or used is None or allocated <= 0:
            return None
        return round(float(used / allocated) * 100, 1)

    @property
    def has_readings(self) -> bool:
        """True when the platform reported at least one usable figure.

        A payload with no numbers *and* no status is an empty answer: the plan has almost
        certainly not started. It is reported as "nothing yet", never as zero usage.
        """
        return any(
            value is not None
            for value in (self.allocated, self.used, self.remaining, self.status, self.expiry)
        )


def parse_consumption(data: Any) -> BackendConsumption | None:
    """Parse a consumption payload, or return ``None`` when it cannot be read.

    ``None`` from the backend is a normal answer -- the platform sends a successful envelope
    with no data when the provider has nothing -- and it is not distinguished from an
    unreadable body here, because the caller treats both the same way: no usage is reported,
    and none is guessed at.
    """
    if not isinstance(data, dict):
        if data is not None:
            logger.warning("consumption_payload_not_an_object")
        return None
    try:
        return BackendConsumption.model_validate(data)
    except Exception:  # pragma: no cover - defensive; every field is already optional
        logger.warning("consumption_payload_unreadable")
        return None
