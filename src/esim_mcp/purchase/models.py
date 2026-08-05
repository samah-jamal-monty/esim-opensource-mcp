"""Typed records for a prepared purchase quote.

A *quote* in this phase is an MCP-local object and nothing else. It records what the user
picked and what the platform said a moment ago; it reserves nothing, holds no stock, and has
no counterpart anywhere in the eSIM backend. There is no backend ``user_order`` row behind a
quote, which is why :class:`PurchaseQuote` carries no order id, payment reference, Stripe
identifier or provisioning field -- there is nothing for them to point at.

What a quote may never hold
---------------------------
No access token, refresh token, OTP, Stripe client secret, card detail, Supabase credential
or MCP transport credential. ``model_config`` is ``extra="forbid"`` so a field cannot be
added by an unvalidated payload, and the field set is asserted by the test suite rather than
left to review. Tokens live in the session store only, and the quote refers to its owner
through digests and an opaque user reference instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

#: Version stamped into every quote. A stored quote from an older layout can be recognized
#: and discarded rather than misread once the store becomes durable (encrypted Redis).
QUOTE_SCHEMA_VERSION = 1

#: Money is rendered with two decimals, the way the backend's own price display does it.
_MONEY_QUANTUM = Decimal("0.01")


def utc_now() -> datetime:
    """Timezone-aware current time (single source, easy to patch in tests)."""
    return datetime.now(tz=UTC)


def money_text(amount: Decimal) -> str:
    """Two-decimal string for a monetary amount. Strings, never floats, cross the wire."""
    return str(amount.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP))


class PaymentMethod(StrEnum):
    """Payment methods that can be *prepared* in this phase.

    The backend's own ``PaymentTypeEnum`` also has ``DCB``, which is deliberately not
    offered here: it is out of scope for Phase 3 and preparing it would imply a carrier
    billing flow this server cannot describe honestly.
    """

    WALLET = "Wallet"
    CARD = "Card"


class QuoteStatus(StrEnum):
    """Lifecycle of a local quote.

    ``CONSUMED`` exists for the execution phase, which will mark a quote used at the moment
    it is exchanged for a real order. Nothing in Phase 3 sets it, because nothing in Phase 3
    can execute a purchase.
    """

    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    CONSUMED = "consumed"


class SearchContextKind(StrEnum):
    """Which catalogue search the user's selection came out of."""

    COUNTRY = "country"
    REGION = "region"
    NONE = "none"


class SearchContext(BaseModel):
    """The catalogue context a selection came from, kept for a future ``related_search``.

    The backend's ``AssignRequest.related_search`` takes either a region
    (``iso_code`` + ``region_name``) or a list of countries (``iso3_code`` +
    ``country_name``). Those authoritative values are captured here at preparation time so
    the execution phase does not have to re-derive them from a display name.

    ``is_complete`` is ``False`` when the caller supplied no context, or when the context it
    supplied could not be confirmed against the platform's own lists. A quote is still
    perfectly valid in that state -- it simply records that the execution phase will have to
    establish the context itself.
    """

    model_config = ConfigDict(extra="forbid")

    kind: SearchContextKind = SearchContextKind.NONE
    country_name: str | None = None
    country_iso3: str | None = None
    region_name: str | None = None
    region_code: str | None = None
    is_complete: bool = False


class QuotedBundle(BaseModel):
    """The authoritative plan facts, as the platform reported them at preparation time.

    Every field here is copied from a fresh ``GET /bundles/{bundle_code}`` response. None of
    it may originate from the model, from the user, or from an earlier catalogue result.
    """

    model_config = ConfigDict(extra="forbid")

    bundle_code: str
    name: str
    data_display: str
    unlimited: bool
    validity_display: str
    validity_days: int | None = None
    plan_type: str | None = None
    activation_policy: str | None = None
    countries_count: int = 0


class QuotedPrice(BaseModel):
    """The displayed catalogue amount. Not a final payable amount, and never treated as one.

    The backend derives this from an exchange rate and it may exclude final payment tax.
    Final tax and the final payable amount are only knowable at checkout, which this phase
    does not reach -- hence the two explicit ``*_confirmed`` flags, which are always
    ``False`` here.
    """

    model_config = ConfigDict(extra="forbid")

    displayed_amount: Decimal
    currency: str
    final_amount_confirmed: bool = False
    tax_included_confirmed: bool = False


class QuotedWallet(BaseModel):
    """A snapshot of the wallet at preparation time. Only present for a Wallet quote.

    A snapshot, not a hold: nothing about this reserves or ring-fences the balance, and the
    real balance may change a second later.
    """

    model_config = ConfigDict(extra="forbid")

    balance: Decimal
    currency: str
    sufficient: bool
    estimated_remaining_balance: Decimal | None = None
    shortfall: Decimal | None = None


class PurchaseQuote(BaseModel):
    """One prepared quote, owned by exactly one MCP client and one eSIM session.

    Ownership is two-part on purpose. ``owner_session_key`` is the digest of the *verified*
    MCP client identity, and ``owner_user_ref`` is a digest of the authenticated eSIM user
    id. A quote is only ever handed back when both match the current caller, so the same
    MCP client signed in as a different user cannot reach the previous user's quotes.
    """

    model_config = ConfigDict(extra="forbid")

    quote_id: str
    schema_version: int = QUOTE_SCHEMA_VERSION
    owner_session_key: str
    owner_user_ref: str
    identity_source: str
    bundle: QuotedBundle
    payment_method: PaymentMethod
    price: QuotedPrice
    wallet: QuotedWallet | None = None
    search_context: SearchContext = Field(default_factory=SearchContext)
    locale: str
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    status: QuoteStatus = QuoteStatus.ACTIVE

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utc_now()) >= self.expires_at

    def effective_status(self, now: datetime | None = None) -> QuoteStatus:
        """Status with expiry applied, so an unswept quote never looks active."""
        if self.status is QuoteStatus.ACTIVE and self.is_expired(now):
            return QuoteStatus.EXPIRED
        return self.status

    def is_usable(self, now: datetime | None = None) -> bool:
        return self.effective_status(now) is QuoteStatus.ACTIVE

    def seconds_remaining(self, now: datetime | None = None) -> int:
        return max(0, int((self.expires_at - (now or utc_now())).total_seconds()))

    def equivalent_to(self, *, bundle_code: str, payment_method: PaymentMethod) -> bool:
        """True when a new request is for the same plan and the same payment method."""
        return self.bundle.bundle_code == bundle_code and self.payment_method is payment_method


def expiry_from(created_at: datetime, ttl_seconds: int) -> datetime:
    """Expiry for a quote created at ``created_at``."""
    return created_at + timedelta(seconds=ttl_seconds)
