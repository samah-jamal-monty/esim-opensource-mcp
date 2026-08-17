"""What one ``get_my_esims`` answer remembers so a later "number 2" can mean something.

The problem this solves is small and entirely presentational until it isn't. An ICCID is a
credential-shaped identifier: it is the handle to a provisioned SIM, and this server is
careful never to read one out to a user or write one to a log. But a user who owns fourteen
eSIMs still has to be able to say *which* one they mean, and "the second one" is how a person
actually says it.

So the list is numbered when it is shown, and the numbers are remembered here alongside the
full identifiers the user never sees. A follow-up call passes the number; this record turns it
back into the ICCID.

What a record is scoped to, and why both halves matter
------------------------------------------------------
Every record carries a :class:`~esim_mcp.purchase.store.QuoteOwner` -- the verified MCP client
identity *and* a digest of the authenticated eSIM user. Not one or the other:

* the **session key** stops one MCP client resolving a number against another client's list;
* the **user reference** stops a number surviving a change of account on the *same* client.
  Sign out and back in as somebody else and the user reference differs, so the stored list
  stops resolving and the caller is told to list the eSIMs again -- which is the only honest
  answer, because "number 2" now refers to a different person's SIM.

Neither half is a raw principal or a backend user id; both are digests, and both are compared
in constant time by the owner type itself.

What a record deliberately does not hold
-----------------------------------------
The install credentials. ``activation_code``, ``qr_code_value`` and ``smdp_address`` claim an
eSIM profile, and an eSIM profile installs exactly once. They are returned to the owner by
``get_my_esims`` because retrieving them is the point of that tool, and they stop there. A
selection record is longer-lived than a single tool result, so it holds the fields needed to
*name* a SIM and to look it up, and nothing that could install one.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from esim_mcp.errors import EsimSelectionOutOfRangeError
from esim_mcp.models.account import BackendEsim

#: How many trailing ICCID digits a numbered listing may show. Four, matching the masking
#: used everywhere else in this server (``****6789``).
ICCID_VISIBLE_TAIL = 4


def utc_now() -> datetime:
    return datetime.now(UTC)


def iccid_last4(value: str | None) -> str | None:
    """The last four digits of an ICCID, or ``None`` when there is nothing safe to show.

    A short value is destroyed rather than partially shown: a string too short to have a
    four-digit tail is not an ICCID, and showing it whole is exactly the failure this
    function exists to prevent.
    """
    if not value:
        return None
    digits = str(value).strip()
    if len(digits) <= ICCID_VISIBLE_TAIL:
        return None
    return digits[-ICCID_VISIBLE_TAIL:]


def require_esim_number(value: int) -> int:
    """Reject a number that could not be a position in any listing, before any lookup.

    Shared by every tool that accepts a selection so "0" and "-1" are refused identically
    everywhere, and so the refusal happens before the store is touched: a number below 1 is
    not a stale listing, it is a bad argument, and the two get different answers.
    """
    if value < 1:
        raise EsimSelectionOutOfRangeError(
            "eSIM numbers start at 1. Show the numbered list from get_my_esims again and use one of its numbers."
        )
    return value


class SelectedEsim(BaseModel):
    """One numbered entry: what to show the user, and what to look the SIM up with.

    ``iccid`` is the full identifier and is the one field here that must never be displayed.
    It exists so a follow-up call can reach the platform; :attr:`last4` is what a numbered
    list shows instead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int = Field(ge=1)
    iccid: str
    #: Everything below is copied from the platform's own ``EsimBundleResponse`` and is only
    #: ever the platform's own words. Nothing here is derived, inferred or formatted into a
    #: claim the platform did not make.
    label_name: str | None = None
    plan_name: str | None = None
    bundle_code: str | None = None
    data_allowance: str | None = None
    validity: str | None = None
    order_status: str | None = None
    plan_started: bool | None = None
    bundle_expired: bool | None = None
    topup_allowed: bool | None = None
    validity_date: str | None = None
    purchased_at: str | None = None

    @property
    def last4(self) -> str | None:
        """The only form of the identifier a numbered listing may show."""
        return iccid_last4(self.iccid)

    def to_backend_esim(self) -> BackendEsim:
        """Rebuild the subset of the platform record the downstream tools read.

        Deliberately a reconstruction rather than a stored :class:`BackendEsim`: it keeps the
        install credentials out of this store by construction, and it lets the consumption and
        top-up tools go on using the *same* reference-shaping and top-up-eligibility helpers
        they use for a freshly-read eSIM, so a selected SIM and a listed one cannot drift into
        producing differently-shaped results.
        """
        return BackendEsim(
            iccid=self.iccid,
            label_name=self.label_name,
            order_status=self.order_status,
            plan_started=self.plan_started,
            bundle_expired=self.bundle_expired,
            is_topup_allowed=self.topup_allowed,
            validity_date=self.validity_date,
            payment_date=None,
            bundle_code=self.bundle_code,
            bundle_marketing_name=self.plan_name,
            gprs_limit_display=self.data_allowance,
            validity_display=self.validity,
        )


class EsimSelectionList(BaseModel):
    """The numbered listing one ``get_my_esims`` call produced, for one owner.

    One per owner: a new listing replaces the previous one outright, so a number always means
    what the user was last shown and never what they were shown before that.
    """

    model_config = ConfigDict(extra="forbid")

    owner_session_key: str
    owner_user_ref: str
    entries: list[SelectedEsim] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def count(self) -> int:
        return len(self.entries)

    def entry(self, number: int) -> SelectedEsim | None:
        """The entry with this number, or ``None`` when the number is outside the listing."""
        for entry in self.entries:
            if entry.number == number:
                return entry
        return None
