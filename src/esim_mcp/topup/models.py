"""Typed records for a prepared top-up, of either kind.

A *quote* here is an MCP-local object and nothing else, exactly as a purchase quote is. It
records what the user picked and what the platform said a moment ago; it reserves nothing,
holds no stock, holds no money and has no counterpart in the eSIM backend.

What a quote may never hold
---------------------------
No access token, refresh token, OTP, Stripe client secret, card detail or MCP transport
credential. ``model_config`` is ``extra="forbid"`` throughout, so a field cannot be added by
an unvalidated payload, and the field sets are asserted by the test suite.

The ICCID is the one sensitive value that legitimately lives here: a top-up quote is *about*
a particular SIM, so the quote has to name it. It is stored raw because the platform needs
it back verbatim, and it is only ever returned to a client **masked**
(:func:`~esim_mcp.topup.models.mask_iccid`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from esim_mcp.purchase.models import QuotedWallet

#: Version stamped into every quote, so a stored record from an older layout can be
#: recognized and discarded rather than misread once a store becomes durable.
TOPUP_QUOTE_SCHEMA_VERSION = 1

#: How many trailing digits of an ICCID a user is shown. An ICCID is 19-20 digits and is the
#: handle to a provisioned SIM, so it is never returned in full -- four digits is enough for
#: a person to recognize which of their SIMs is being talked about and useless to anyone else.
_ICCID_VISIBLE_TAIL = 4


def utc_now() -> datetime:
    """Timezone-aware current time (single source, easy to patch in tests)."""
    return datetime.now(tz=UTC)


def mask_iccid(value: str | None) -> str | None:
    """``8931080019123456789`` -> ``****6789``.

    The only form in which an ICCID leaves this server. Short values are destroyed rather
    than partially shown, because a short "ICCID" is not one and showing it whole would be
    the failure this function exists to prevent.
    """
    if not value:
        return None
    digits = str(value).strip()
    if len(digits) <= _ICCID_VISIBLE_TAIL:
        return "*" * len(digits)
    return f"****{digits[-_ICCID_VISIBLE_TAIL:]}"


class TopupQuoteStatus(StrEnum):
    """Lifecycle of a local top-up quote.

    ``CONSUMED`` is reached only when a quote has been exchanged for something real. For a
    wallet top-up that is a hosted page the user has paid; for an eSIM top-up nothing in
    this version can reach it, because nothing in this version executes one.
    """

    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    CONSUMED = "consumed"


class TopupStatus(StrEnum):
    """Lifecycle of one wallet top-up's hosted page, as this server tracks it locally.

    ``OPEN`` is terminal for *creation*: a page exists, so the stored answer is replayed and
    nothing is sent. It says nothing about whether the user paid -- only
    :class:`~esim_mcp.models.wallet_topup.WalletTopupStatus`, read from the platform, can.
    """

    PENDING = "pending"
    OPEN = "open"
    FAILED = "failed"
    UNRESOLVED = "unresolved"

    @property
    def is_terminal(self) -> bool:
        """True when the stored answer must be replayed rather than sent again."""
        return self in (TopupStatus.OPEN, TopupStatus.FAILED)


class EsimTarget(BaseModel):
    """The eSIM a top-up is for, resolved from the caller's *own* ``get_my_esims`` list.

    Every field is copied from the platform's own record of an eSIM this user owns. None of
    it may originate from the model or from the user: that is what makes a quote's target
    provably one of the caller's SIMs rather than an identifier somebody typed.
    """

    model_config = ConfigDict(extra="forbid")

    #: Raw, because the platform needs it back verbatim. Only ever returned masked.
    iccid: SecretStr
    masked_iccid: str
    #: The plan currently on the SIM, which is what the platform matches top-ups against.
    bundle_code: str
    bundle_name: str | None = None
    plan_started: bool | None = None
    bundle_expired: bool | None = None
    topup_allowed: bool | None = None
    label: str | None = None
    order_reference: str | None = None


class QuotedTopupBundle(BaseModel):
    """The authoritative top-up plan facts, as the platform reported them at quote time.

    Copied from a fresh ``GET /user/related-topup/{bundle_code}/{iccid}`` read. None of it
    may come from the general catalogue: only the platform can say what is compatible with a
    SIM already in use, and a catalogue plan that merely looks similar is not a top-up.
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


class QuotedTopupPrice(BaseModel):
    """The amount the platform priced this top-up at, in the platform's own currency."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal
    currency: str


class EsimTopupQuote(BaseModel):
    """One prepared eSIM top-up, owned by exactly one MCP client and one eSIM session.

    Ownership is two-part, exactly as a purchase quote's is: ``owner_session_key`` is the
    digest of the verified MCP client identity and ``owner_user_ref`` is a digest of the
    authenticated eSIM user id. A quote is only ever handed back when both match, so the
    same MCP client signed in as a different user cannot reach the previous user's quotes --
    and therefore cannot reach the previous user's SIM.
    """

    model_config = ConfigDict(extra="forbid")

    quote_id: str
    schema_version: int = TOPUP_QUOTE_SCHEMA_VERSION
    owner_session_key: str
    owner_user_ref: str
    identity_source: str
    target: EsimTarget
    bundle: QuotedTopupBundle
    price: QuotedTopupPrice
    payment_method: str
    #: The wallet at quote time. A snapshot, not a hold: the real balance may change a
    #: second later, which is why the execution path re-reads it before spending anything.
    wallet: QuotedWallet | None = None
    locale: str
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    status: TopupQuoteStatus = TopupQuoteStatus.ACTIVE

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utc_now()) >= self.expires_at

    def effective_status(self, now: datetime | None = None) -> TopupQuoteStatus:
        """Status with expiry applied, so an unswept quote never looks active."""
        if self.status is TopupQuoteStatus.ACTIVE and self.is_expired(now):
            return TopupQuoteStatus.EXPIRED
        return self.status

    def seconds_remaining(self, now: datetime | None = None) -> int:
        return max(0, int((self.expires_at - (now or utc_now())).total_seconds()))


class EsimTopupExecutionStatus(StrEnum):
    """Lifecycle of one eSIM top-up execution.

    Every state except ``PENDING`` is **terminal**, and that is the whole design. Elsewhere
    in this codebase an unresolved outcome may be presented to the platform again with the
    key it already used, so the platform can say what really happened. The legacy top-up
    route has no key: asking again is not a question about the first request, it is a second
    top-up. So ``UNRESOLVED`` locks the quote exactly as ``SUCCEEDED`` does.
    """

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNRESOLVED = "unresolved"

    @property
    def is_terminal(self) -> bool:
        return self is not EsimTopupExecutionStatus.PENDING


class EsimTopupExecution(BaseModel):
    """One quote's single execution attempt.

    There is deliberately **no idempotency key** on this record. The platform accepts none,
    so minting one would be theatre: it would travel nowhere and protect nothing. What this
    record holds instead is the fact that an attempt *was made*, which is the only thing that
    can make a non-idempotent write survivable -- one quote, one attempt, whatever happened.
    """

    model_config = ConfigDict(extra="forbid")

    quote_id: str
    owner_session_key: str
    owner_user_ref: str
    attempts: int = 0
    status: EsimTopupExecutionStatus = EsimTopupExecutionStatus.PENDING
    #: The safe result payload replayed to a caller that asks again.
    result: dict[str, object] | None = None
    #: The typed error a terminal failure or an unresolved outcome replays.
    failure_code: str | None = None
    failure_message: str | None = None
    failure_details: dict[str, object] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def was_sent(self) -> bool:
        """True once a request has left this process for this quote.

        Counted *before* the send, so a request that died in flight still counts. That is
        the conservative direction: an attempt whose fate is unknown must lock the quote just
        as firmly as one that succeeded.
        """
        return self.attempts > 0


class WalletTopupQuote(BaseModel):
    """One prepared wallet top-up: an amount the platform has confirmed it would accept.

    Deliberately carries no order id, payment reference or provider identifier -- there is
    nothing at the platform behind a quote for them to point at. The first thing that exists
    at the platform is the order the checkout call creates.
    """

    model_config = ConfigDict(extra="forbid")

    quote_id: str
    schema_version: int = TOPUP_QUOTE_SCHEMA_VERSION
    owner_session_key: str
    owner_user_ref: str
    identity_source: str
    amount: Decimal
    currency: str
    #: The balance at quote time. A snapshot, not a hold.
    balance: Decimal | None = None
    minimum_amount: Decimal | None = None
    maximum_amount: Decimal | None = None
    locale: str
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    status: TopupQuoteStatus = TopupQuoteStatus.ACTIVE

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utc_now()) >= self.expires_at

    def effective_status(self, now: datetime | None = None) -> TopupQuoteStatus:
        if self.status is TopupQuoteStatus.ACTIVE and self.is_expired(now):
            return TopupQuoteStatus.EXPIRED
        return self.status

    def seconds_remaining(self, now: datetime | None = None) -> int:
        return max(0, int((self.expires_at - (now or utc_now())).total_seconds()))


class WalletTopupCheckout(BaseModel):
    """One wallet-top-up quote's checkout history.

    .. important::
       This record is a **cache, not a safety mechanism.** Duplicate protection for a wallet
       top-up lives at the platform, which recognizes a repeat from the caller's own pending
       ``user_order`` row and answers with the page it already opened -- a guarantee that
       survives a restart of this process, a redeploy and a move between replicas, none of
       which this record does. What it buys is that a repeated call usually does not need to
       leave the process at all, and that a payment reference can be scoped to the client
       that received it.

    It holds no idempotency key of its own, precisely so nothing here can be mistaken for
    the durable guarantee: there is no key to mint, and the platform is not asked to trust one.
    """

    model_config = ConfigDict(extra="forbid")

    quote_id: str
    owner_session_key: str
    owner_user_ref: str
    attempts: int = 0
    checks: int = 0
    status: TopupStatus = TopupStatus.PENDING
    #: The safe checkout payload replayed to the caller once a page exists.
    result: dict[str, object] | None = None
    #: The platform's reference for the top-up, used for every later status read.
    payment_reference: str | None = None
    #: The locale and currency the quote was priced in, so a later status read asks the
    #: platform in the same terms the user was quoted in.
    locale: str | None = None
    currency: str | None = None
    #: The safe payload of a *terminal* status, replayed instead of re-read.
    payment_result: dict[str, object] | None = None
    #: The typed error a terminal failure replays.
    failure_code: str | None = None
    failure_message: str | None = None
    failure_details: dict[str, object] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def attempts_remaining(self) -> int:
        return max(0, MAX_TOPUP_CHECKOUT_ATTEMPTS - self.attempts)

    @property
    def checks_remaining(self) -> int:
        return max(0, MAX_TOPUP_STATUS_CHECKS - self.checks)


#: How many times one quote may be sent for checkout before this server refuses to try
#: again. A guard against an unattended retry loop, not against a duplicate page: the
#: platform is what prevents a second page.
MAX_TOPUP_CHECKOUT_ATTEMPTS = 3

#: How many times one top-up may be checked before this server stops asking. Terminal
#: answers are replayed from the local record and never count.
MAX_TOPUP_STATUS_CHECKS = 20


def expiry_from(created_at: datetime, ttl_seconds: int) -> datetime:
    """Expiry for a quote created at ``created_at``."""
    return created_at + timedelta(seconds=ttl_seconds)
