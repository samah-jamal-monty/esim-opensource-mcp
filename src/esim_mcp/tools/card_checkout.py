"""Phase 5B MCP tools: paying for a prepared plan by card, on the platform's hosted page.

This is the second way a user can pay in this server, and it is the only one where the money
does **not** move through anything this process controls. That distinction shapes every rule
below.

Where the card goes
-------------------
**Nowhere near here.** The user enters their card on the payment provider's own hosted
checkout page, in their own browser. This server never renders that page, never proxies it and
never receives its contents; there is no argument on either tool for a card number, an expiry,
a security code, a cardholder name or a payment token, and no code path that could accept one.
An assistant must therefore never ask for card details -- not to "pass them on", not to "check
them", not for any reason -- and the tool guidance says so in as many words.

What ``create_card_checkout`` may take from the model
-----------------------------------------------------
**One argument: the reference of a Card quote this same caller prepared.** The plan, the
amount, the currency, the destination context and the user all come from the *stored quote*,
so a hallucinated amount, a swapped bundle code or an invented user has no way into the
request. There is no amount argument to poison and no payment-method argument through which a
wallet balance could be spent instead.

What it guarantees about payment pages
--------------------------------------
* **One key per quote.** The ``Idempotency-Key`` is minted once and reused for every later
  attempt on that quote, so a retry -- transport level or model level -- presents the platform
  with the key it already knows and receives the *existing* page.
* **One page per quote.** Once a checkout exists, its stored result is replayed and no request
  is sent. A user is never handed two links for one plan.
* **A link is validated, not trusted.** Only a plain ``https`` address is ever passed on, and a
  page this server cannot later identify is not shown at all.

Who is allowed to say a payment happened
----------------------------------------
**The backend's existing signature-verified payment webhook, and nothing else.** Stripe calls
that webhook; the webhook settles the payment and triggers provisioning. This server is a
*reader* of the result and has no part in producing it: it never calls the webhook, never
simulates one, never marks a payment paid, never provisions, and holds no Stripe credential
with which it could do any of those things. Every "paid" and "provisioned" in this module is
copied from a status read, never decided here.

What ``check_card_payment_status`` will and will not conclude
------------------------------------------------------------
Only a status read from the platform can say whether money arrived. A browser redirect, a
"success" page, a returning user or the user simply saying they paid are **not** evidence, and
the tool guidance names each of those explicitly. There is no argument through which the model
could assert that a payment happened, and no branch that infers one.

Checking is bounded and never automatic: one backend read per call, and a documented cap
(:data:`~esim_mcp.purchase.card.MAX_STATUS_CHECKS`) past which this server stops asking. A
terminal answer is stored and replayed rather than re-read.

What neither tool ever returns
------------------------------
No access token, refresh token, idempotency key, key fingerprint, provider session id, client
secret, publishable key, card detail, backend correlation id or developer message. The results
carry the link, the amount, the currency, the payment reference, the expiry, the payment state
and -- once there is one -- the order id.
"""

from __future__ import annotations

import contextlib
import logging
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from esim_mcp.client.card import CardCheckoutApiClient, require_payment_reference
from esim_mcp.errors import (
    AuthenticationRequiredError,
    BackendTimeoutError,
    BackendUnavailableError,
    BundleUnavailableError,
    CardCheckoutOutcomeUnknownError,
    CardCheckoutRejectedError,
    CardCheckoutUnavailableError,
    CardPaymentAmbiguousError,
    CardPaymentStatusUnavailableError,
    EsimMcpError,
    IdempotencyConflictError,
    InvalidInputError,
    NonCardQuoteError,
    PurchaseCurrencyMismatchError,
    RateLimitedError,
    UnsafeCheckoutLinkError,
)
from esim_mcp.models.card import (
    BackendCardCheckout,
    BackendCardPaymentStatus,
    CardPaymentStatus,
    parse_card_checkout,
    parse_card_payment_status,
    safe_checkout_url,
)
from esim_mcp.models.wallet import decimal_from_number
from esim_mcp.purchase.card import (
    CardCheckout,
    CardCheckoutService,
    CheckoutStatus,
    replay_error,
    require_attempts_remaining,
    require_card_quote,
    require_checks_remaining,
)
from esim_mcp.purchase.execution import key_fingerprint
from esim_mcp.purchase.models import PurchaseQuote, money_text
from esim_mcp.purchase.service import PurchaseQuoteService, quote_ref
from esim_mcp.purchase.store import QuoteOwner
from esim_mcp.purchase.validation import require_usable_quote
from esim_mcp.session.identity import ClientIdentityProvider, derive_device_id
from esim_mcp.session.manager import SessionManager
from esim_mcp.settings import Settings
from esim_mcp.tools.guard import guarded
from esim_mcp.tools.purchase_preparation import user_ref_of

logger = logging.getLogger(__name__)

#: The sentence a fresh checkout result carries. It says what exists -- a page -- and, just as
#: importantly, what does not: a payment.
CHECKOUT_READY = "A secure payment page was opened for this plan. Nothing has been charged yet."

#: What the assistant should do once a link exists.
CHECKOUT_NEXT_STEP = (
    "Give the user the payment link and tell them the amount and the plan. Say plainly that nothing has been "
    "charged yet and that they enter their card only on Stripe's secure page, which the link opens. Never ask "
    "them for a card number, an expiry date, a security code or any other card detail, and never offer to enter "
    "one for them. The platform has recorded an UNPAID order behind this page; it becomes a real purchase only "
    "once the user pays on Stripe, so never describe it as bought, placed or reserved. Then wait: do not check "
    "the payment until they say they have paid, or ask you to check."
)

#: Repeated on a replayed checkout so the assistant does not describe one page as two.
CHECKOUT_REPLAY_NOTE = (
    "This payment page already existed and this is the same link, not a second one. The user is being asked to "
    "pay once. Never tell them a new page was created or that they must pay again."
)

#: The rule that has to survive into every card result, in every branch.
CARD_ENTRY_NOTE = (
    "Card details are entered ONLY on Stripe's own secure hosted payment page, which opens in the user's browser "
    "at the link above. This assistant never sees, asks for, stores or forwards a card number, an expiry date or "
    "a security code. If the user offers card details in the conversation, tell them not to and point them at "
    "the link instead."
)

#: The single mistake this phase most has to prevent, repeated wherever a status is reported.
REDIRECT_IS_NOT_PROOF = (
    "A browser redirect back from the payment page, a success screen, the user returning to this conversation, or "
    "the user saying they paid are NOT proof of payment. Only a payment status read from the eSIM platform with "
    "check_card_payment_status can confirm it."
)

QuoteReferenceArg = Annotated[
    str,
    Field(
        description=(
            "The quote reference returned by prepare_purchase in this conversation, for the Card "
            "quote the user has just explicitly agreed to pay. Never invent, guess or edit one, "
            "and never take one from the user -- use the reference from your own prepare_purchase "
            "result. If you do not have one, or it has expired, prepare the plan again for card "
            "payment and ask the user to confirm the new amount before calling this."
        )
    ),
]

PaymentReferenceArg = Annotated[
    str,
    Field(
        description=(
            "The payment reference returned by create_card_checkout in this conversation. Never "
            "invent, guess or edit one, and never take one from the user. There is no argument "
            "here for telling this tool that the payment succeeded: whether it did is read from "
            "the eSIM platform and from nowhere else."
        )
    ),
]


class CardPaymentService:
    """Backend-facing behaviour for the two card-checkout tools."""

    def __init__(
        self,
        settings: Settings,
        card_client: CardCheckoutApiClient,
        session_manager: SessionManager,
        identity_provider: ClientIdentityProvider,
        quotes: PurchaseQuoteService,
        checkouts: CardCheckoutService,
    ) -> None:
        self._settings = settings
        self._cards = card_client
        self._sessions = session_manager
        self._identity_provider = identity_provider
        self._quotes = quotes
        self._checkouts = checkouts

    # ------------------------------------------------------------------ internals

    async def _owner(self, ctx: Any | None) -> tuple[str, QuoteOwner]:
        """Resolve the caller and require a signed-in eSIM user.

        The owner key is built exactly as ``prepare_purchase`` builds it, from the verified MCP
        client identity plus a digest of the authenticated user id, so a quote prepared by one
        client-and-user pair can only ever be paid by that same pair -- and a payment started
        by one pair can only ever be read by it.
        """
        identity = await self._identity_provider.resolve(ctx)
        device_id = derive_device_id(self._settings.salt_bytes(), identity)
        session = await self._sessions.require_session(identity.session_key)
        return device_id, QuoteOwner(session_key=identity.session_key, user_ref=user_ref_of(session))

    # ------------------------------------------------------------- create_card_checkout

    async def create_card_checkout(self, *, quote_reference: str, ctx: Any | None = None) -> dict[str, Any]:
        """Open a hosted payment page for a prepared Card quote. Charges nothing."""
        device_id, owner = await self._owner(ctx)
        reference = _require_quote_reference(quote_reference)

        # Held across the backend call on purpose: two calls for the same quote must not both
        # reach the platform, whatever order the client sends them in.
        async with self._checkouts.lock(reference):
            quote = await self._quotes.get_record(owner, reference)
            checkout = await self._checkouts.acquire(owner, reference)

            replayed = self._replay_if_terminal(checkout, quote)
            if replayed is not None:
                return replayed

            # Only reached when nothing terminal is recorded, so these gates can never reject
            # a quote whose payment page already exists.
            require_usable_quote(quote)
            require_card_quote(quote)
            require_attempts_remaining(checkout)

            return await self._open_checkout(quote=quote, checkout=checkout, device_id=device_id)

    def _replay_if_terminal(self, checkout: CardCheckout, quote: PurchaseQuote) -> dict[str, Any] | None:
        """Hand back the stored answer for a checkout that already reached one.

        Returning the *stored* link rather than asking the platform again is the whole point:
        a repeated call must not open a second page, and must not read to the user as though
        they now owe two payments.
        """
        if checkout.status is CheckoutStatus.OPEN and checkout.result is not None:
            logger.info("card_checkout_replayed_locally", extra={"quote_ref": quote_ref(quote.quote_id)})
            return {**checkout.result, "replayed": True, "replay_note": CHECKOUT_REPLAY_NOTE}
        if checkout.status.is_terminal:
            raise replay_error(checkout)
        return None

    async def _open_checkout(
        self,
        *,
        quote: PurchaseQuote,
        checkout: CardCheckout,
        device_id: str,
    ) -> dict[str, Any]:
        """Send exactly one checkout request for ``quote`` and interpret the answer."""
        # Counted before the call, so an attempt that dies mid-flight is still an attempt.
        checkout = await self._checkouts.record_attempt(checkout)

        # Refreshed *before* the call rather than replayed after a 401: a replay would be a
        # second POST, and this is not a call to repeat on this server's own initiative.
        session = await self._sessions.ensure_fresh_session(
            checkout.owner_session_key,
            device_id=device_id,
            locale=quote.locale,
            currency=quote.price.currency,
        )

        logger.info(
            "card_checkout_sending",
            extra={
                "quote_ref": quote_ref(quote.quote_id),
                "key_fp": key_fingerprint(checkout.idempotency_key),
                "attempt": checkout.attempts,
            },
        )

        try:
            outcome = await self._cards.create_checkout(
                device_id=device_id,
                access_token=session.access_token,
                idempotency_key=checkout.idempotency_key,
                bundle_code=quote.bundle.bundle_code,
                quote_reference=quote.quote_id,
                search_context=quote.search_context,
                locale=quote.locale,
                currency=quote.price.currency,
            )
        except (BackendTimeoutError, BackendUnavailableError):
            # A page may or may not have been opened. Nobody was charged either way -- the
            # user has not seen a link, so nothing can have been paid on it.
            await self._checkouts.mark_unresolved(checkout, CardCheckoutOutcomeUnknownError())
            logger.warning("card_checkout_outcome_unknown", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise CardCheckoutOutcomeUnknownError(details=_unknown_details(checkout)) from None

        if outcome.is_success:
            return await self._on_checkout_success(data=outcome.data, quote=quote, checkout=checkout)

        raise await self._on_checkout_failure(
            outcome_status=outcome.effective_status,
            hint=outcome.error_hint,
            parsed=outcome.parsed,
            quote=quote,
            checkout=checkout,
        )

    async def _on_checkout_success(
        self,
        *,
        data: Any,
        quote: PurchaseQuote,
        checkout: CardCheckout,
    ) -> dict[str, Any]:
        """Interpret a successful envelope. Only a usable, trackable link counts as a page."""
        parsed = parse_card_checkout(data)
        if parsed is None:
            await self._checkouts.mark_unresolved(checkout, CardCheckoutOutcomeUnknownError())
            logger.warning("card_checkout_envelope_unreadable", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise CardCheckoutOutcomeUnknownError(details=_unknown_details(checkout))

        link = safe_checkout_url(parsed.checkout_url)
        if link is None:
            # Never shown, never repaired. A refused link is a refusal to send the user
            # anywhere, which costs them nothing.
            await self._checkouts.mark_failed(checkout, UnsafeCheckoutLinkError())
            logger.error("card_checkout_link_refused", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise UnsafeCheckoutLinkError()

        try:
            reference = require_payment_reference(parsed.payment_reference)
        except InvalidInputError:
            # A page this server could not identify afterwards is worse than no page: the user
            # would pay with no way for anyone here to confirm it. So it is not handed over.
            error = CardCheckoutRejectedError(
                "The eSIM platform did not return a usable reference for this payment, so the link was not shown "
                "and nothing was charged. Tell the user card payment could not be started and offer to pay from "
                "their wallet instead."
            )
            await self._checkouts.mark_failed(checkout, error)
            logger.error("card_checkout_reference_unusable", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise error from None

        payload = _checkout_result(quote, parsed, link=link, payment_reference=reference)
        await self._checkouts.mark_open(
            checkout,
            result=payload,
            payment_reference=reference,
            locale=quote.locale,
            currency=quote.price.currency,
        )
        return {**payload, "replayed": bool(parsed.idempotent_replay)}

    async def _on_checkout_failure(
        self,
        *,
        outcome_status: int,
        hint: str,
        parsed: bool,
        quote: PurchaseQuote,
        checkout: CardCheckout,
    ) -> EsimMcpError:
        """Classify a non-successful answer, record it, and return the error to raise.

        The error is *returned* rather than raised so the recording above it cannot be skipped
        by an early exit, and so the call site reads as the single place a failed checkout
        leaves this service.
        """
        error, disposition = _classify_checkout_failure(outcome_status, hint, parsed)

        if disposition is _Disposition.TERMINAL:
            await self._checkouts.mark_failed(checkout, error)
        elif disposition is _Disposition.UNRESOLVED:
            await self._checkouts.mark_unresolved(checkout, error)
        # _Disposition.RETRYABLE records nothing: the platform refused before opening
        # anything, and the same key stays reserved for the next attempt.

        logger.info(
            "card_checkout_not_opened",
            extra={
                "quote_ref": quote_ref(quote.quote_id),
                "key_fp": key_fingerprint(checkout.idempotency_key),
                "http_status": outcome_status,
                "error_code": error.code,
                "disposition": disposition.value,
                "charged": False,
            },
        )
        return error

    # --------------------------------------------------------- check_card_payment_status

    async def check_card_payment_status(self, *, payment_reference: str, ctx: Any | None = None) -> dict[str, Any]:
        """Read what the platform says happened to one card payment this caller started."""
        device_id, owner = await self._owner(ctx)
        reference = require_payment_reference(payment_reference)

        async with self._checkouts.lock(f"payment:{reference}"):
            # Ownership boundary: a reference this client never received is *not found*, not
            # refused, so nothing here can be used to probe for another client's payments.
            checkout = await self._checkouts.require_by_reference(owner, reference)

            if checkout.payment_result is not None:
                logger.info("card_payment_status_replayed_locally", extra={"quote_ref": quote_ref(checkout.quote_id)})
                return {**checkout.payment_result, "replayed": True}
            if checkout.failure_code == CardPaymentAmbiguousError.code:
                # Recorded by an earlier check. Asking again would be exactly the wrong move.
                raise replay_error(checkout)

            require_checks_remaining(checkout)
            checkout = await self._checkouts.record_check(checkout)

            # Asked in the terms the quote was priced in, so an amount read back to the user
            # is the amount they agreed to rather than a server-default conversion of it.
            locale = checkout.locale or self._settings.default_locale
            currency = checkout.currency or self._settings.default_currency

            session = await self._sessions.ensure_fresh_session(
                checkout.owner_session_key,
                device_id=device_id,
                locale=locale,
                currency=currency,
            )

            try:
                outcome = await self._cards.get_payment_status(
                    device_id=device_id,
                    access_token=session.access_token,
                    payment_reference=reference,
                    locale=locale,
                    currency=currency,
                )
            except (BackendTimeoutError, BackendUnavailableError):
                # A status read takes nothing and changes nothing, so it is safe to repeat --
                # but never safe to guess at.
                logger.warning("card_payment_status_unavailable", extra={"quote_ref": quote_ref(checkout.quote_id)})
                raise CardPaymentStatusUnavailableError() from None

            if not outcome.is_success:
                raise _classify_status_failure(outcome.effective_status)

            return await self._on_status(data=outcome.data, checkout=checkout, owner=owner)

    async def _on_status(
        self,
        *,
        data: Any,
        checkout: CardCheckout,
        owner: QuoteOwner,
    ) -> dict[str, Any]:
        """Interpret one payment status. An unreadable answer is never resolved locally."""
        parsed = parse_card_payment_status(data)
        status = parsed.payment_status if parsed is not None else None
        if parsed is None or status is None:
            logger.warning("card_payment_status_unreadable", extra={"quote_ref": quote_ref(checkout.quote_id)})
            raise CardPaymentStatusUnavailableError()

        if status is CardPaymentStatus.AMBIGUOUS:
            error = CardPaymentAmbiguousError(details={"retry_safe": False, "next_step": "contact_support"})
            await self._checkouts.mark_failed(checkout, error)
            logger.warning("card_payment_ambiguous", extra={"quote_ref": quote_ref(checkout.quote_id)})
            raise error

        payload = _status_result(checkout, parsed, status)

        if status is CardPaymentStatus.COMPLETED:
            # Consumed only here: after the platform confirmed a settled payment and a
            # completed order, so this quote can never become a second order.
            with contextlib.suppress(EsimMcpError):
                await self._quotes.consume(owner, checkout.quote_id)

        if status.is_terminal:
            await self._checkouts.mark_payment_settled(checkout, payload)

        logger.info(
            "card_payment_status_read",
            extra={
                "quote_ref": quote_ref(checkout.quote_id),
                "payment_state": status.value,
                # Read from the platform, never decided here: this server cannot make a
                # payment paid, and the log has to reflect that as plainly as the result does.
                "charged": status.money_arrived,
                "provisioned": bool(parsed.provisioned),
                "check": checkout.checks,
            },
        )
        return payload


# ------------------------------------------------------------------ outcome classification


class _Disposition(StrEnum):
    """How an unsuccessful checkout answer is remembered.

    ``RETRYABLE`` deliberately records nothing: the platform refused before opening anything,
    so the record stays pending and keeps its key for the next attempt.
    """

    TERMINAL = "terminal"
    UNRESOLVED = "unresolved"
    RETRYABLE = "retryable"


#: Backend error labels matched for internal classification only. The platform's envelope has
#: no machine-readable code: ``title`` is a localized string that falls back to the internal
#: error key when no translation exists, so both forms are matched here. Neither is ever
#: forwarded to an MCP client.
_BUSINESS_ERROR_MATCHERS: tuple[tuple[tuple[str, ...], type[EsimMcpError]], ...] = (
    (("bundle_not_available", "bundle not available"), BundleUnavailableError),
    (("unsupported_currency", "unsupported currency"), PurchaseCurrencyMismatchError),
    (("unsupported_payment_type", "unsupported payment"), NonCardQuoteError),
)


def _classify_checkout_failure(status: int, hint: str, parsed: bool) -> tuple[EsimMcpError, _Disposition]:
    """Map one unsuccessful checkout answer to a typed error and how to remember it.

    Every branch here describes something that did *not* take money: a page that failed to
    open cannot have been paid on. That is why none of these carry the "may have been charged"
    wording the wallet path needs -- saying it here would frighten a user for nothing.
    """
    lowered = (hint or "").strip().lower()

    if status in (401, 403):
        return AuthenticationRequiredError(
            "The eSIM session was refused by the platform, so no payment page was opened and nothing was charged. "
            "Ask the user to sign in again, then prepare the plan again and confirm the new amount with them."
        ), _Disposition.RETRYABLE

    if status == 404:
        return CardCheckoutUnavailableError(
            "This eSIM platform does not offer assisted card checkout, so no payment page was opened and nothing "
            "was charged. Tell the user card payment is unavailable here and offer to pay from their wallet."
        ), _Disposition.RETRYABLE

    if status == 429:
        return RateLimitedError(
            "The eSIM platform is rate limiting this client, so no payment page was opened and nothing was "
            "charged. Tell the user and offer to try the same plan again shortly."
        ), _Disposition.RETRYABLE

    if status == 409:
        # "Still processing" is not a conflict: the same key is already doing exactly what this
        # call wanted, so the answer is to ask again with it, never to re-key.
        if "in_progress" in lowered or "in progress" in lowered or "still processing" in lowered:
            return CardCheckoutOutcomeUnknownError(), _Disposition.UNRESOLVED
        return IdempotencyConflictError(
            "The eSIM platform could not match this request to the prepared quote, so no payment page was opened "
            "and nothing was charged. Prepare the plan again and confirm the new amount with the user."
        ), _Disposition.TERMINAL

    if status in (400, 422):
        for needles, error_type in _BUSINESS_ERROR_MATCHERS:
            if any(needle in lowered for needle in needles):
                return error_type(), _Disposition.TERMINAL
        return CardCheckoutRejectedError(), _Disposition.TERMINAL

    if status == 503:
        return CardCheckoutUnavailableError(), _Disposition.RETRYABLE

    if not parsed and 200 <= status < 300:
        return CardCheckoutOutcomeUnknownError(), _Disposition.UNRESOLVED

    if status >= 500:
        return CardCheckoutOutcomeUnknownError(), _Disposition.UNRESOLVED

    return CardCheckoutRejectedError(), _Disposition.TERMINAL


def _classify_status_failure(status: int) -> EsimMcpError:
    """Map one unsuccessful *status read* to a typed error.

    None of these may claim an outcome. A payment whose state could not be read is a payment
    whose state is unknown, and "unknown" is the only honest thing to report -- with one
    exception: a reference the platform does not recognize at all was never a payment, so it
    is reported as not found rather than as an unreadable state.
    """
    if status in (401, 403):
        return AuthenticationRequiredError(
            "The eSIM session was refused by the platform, so this payment could not be checked. Ask the user to "
            "sign in again, then check the same payment. Never assume it succeeded or failed in the meantime."
        )
    if status == 429:
        return RateLimitedError(
            "The eSIM platform is rate limiting this client, so the payment could not be checked. Tell the user "
            "and offer to check the same payment again shortly. Never guess whether it went through."
        )
    return CardPaymentStatusUnavailableError()


def _unknown_details(checkout: CardCheckout) -> dict[str, Any]:
    """Safe payload for a checkout whose outcome this server never learned."""
    return {
        "charged": False,
        "retry_safe": True,
        "new_quote_safe": False,
        "next_step": "retry_same_checkout",
        "attempts_remaining": checkout.attempts_remaining,
    }


# ------------------------------------------------------------------------ result shaping


def _amount_text(value: Any, fallback: Decimal) -> str:
    """Two-decimal string for an amount, preferring the platform's own figure.

    The platform is authoritative for what the card will actually be charged: it applies the
    final tax this server is not able to compute. The quote's displayed amount is used only
    when the platform sent none, and never to *override* one.
    """
    parsed = decimal_from_number(value)
    return money_text(parsed if parsed is not None else fallback)


def _checkout_result(
    quote: PurchaseQuote,
    checkout: BackendCardCheckout,
    *,
    link: str,
    payment_reference: str,
) -> dict[str, Any]:
    """The structured facts an open payment page reports.

    ``charged`` and ``paid`` are ``False`` here unconditionally -- not computed, because there
    is no branch on which opening a page could have taken money.

    ``order_id`` is reported when the platform sent one, because it does: the backend records
    an **unpaid** order alongside the payment page. That is a fact worth carrying (it is what
    a later status read is about) and a dangerous one to misread, so it travels next to
    ``paid: false`` and a ``next_step`` that says in words that the order only becomes a
    purchase when the user pays on Stripe's page.
    """
    payload: dict[str, Any] = {
        "status": "checkout_ready",
        "quote_reference": quote.quote_id,
        "payment_reference": payment_reference,
        "checkout_url": link,
        "payment_method": "Card",
        "amount": _amount_text(checkout.amount, quote.price.displayed_amount),
        "currency": (checkout.currency or quote.price.currency or "").strip().upper(),
        "payment_status": (checkout.payment_status or CardPaymentStatus.PENDING).value,
        "charged": False,
        "paid": False,
        "provisioned": False,
        "bundle": {
            "name": quote.bundle.name,
            "data": quote.bundle.data_display,
            "validity": quote.bundle.validity_display,
        },
        "message": CHECKOUT_READY,
        "card_entry_note": CARD_ENTRY_NOTE,
        "next_step": CHECKOUT_NEXT_STEP,
        "proof_note": REDIRECT_IS_NOT_PROOF,
    }
    if checkout.expires_at:
        payload["expires_at"] = checkout.expires_at
    if checkout.order_id:
        payload["order_id"] = checkout.order_id
        payload["order_state"] = "unpaid"
    if quote.bundle.activation_policy:
        payload["bundle"]["activation_policy"] = quote.bundle.activation_policy
    return payload


#: What to tell the assistant for each payment state. Written as instructions rather than as
#: prose about the state, because the difference between "wait" and "start again" is the whole
#: value of reading a status at all.
_STATUS_GUIDANCE: dict[CardPaymentStatus, str] = {
    CardPaymentStatus.PENDING: (
        "The user has not paid yet. Give them the payment link again and ask them to finish on Stripe's secure "
        "page. Do not check again until they say they have paid. Never ask for card details."
    ),
    CardPaymentStatus.PAID: (
        "The payment was received. The eSIM is not ready yet, so do not say the plan is active or installed. Tell "
        "the user the payment went through and that the platform is finishing the order, and offer to check again "
        "in a minute."
    ),
    CardPaymentStatus.PROVISIONING: (
        "The payment was received and the platform is preparing the eSIM. Do not say it is ready, installed or "
        "activated. Tell the user it is being set up and offer to check again in a minute."
    ),
    CardPaymentStatus.COMPLETED: (
        "The payment went through and the order is complete. Tell the user plainly that the plan was paid for by "
        "card, and give them the plan name and the amount. Do not read the order reference out unless they ask. "
        "Installing or activating the eSIM is not something this version can do: point them to the eSIM app or "
        "their order confirmation email."
    ),
    CardPaymentStatus.FAILED: (
        "The payment did not go through and nothing was charged. Tell the user plainly, and offer to prepare the "
        "plan again and open a new payment page. Never ask them for card details, and never guess why it failed."
    ),
    CardPaymentStatus.EXPIRED: (
        "The payment page expired before it was paid, and nothing was charged. Tell the user, and offer to "
        "prepare the plan again and open a new payment page."
    ),
    CardPaymentStatus.CANCELLED: (
        "The payment was cancelled and nothing was charged. Ask the user whether they still want the plan; if "
        "they do, prepare it again and open a new payment page."
    ),
}


def _status_result(
    checkout: CardCheckout,
    status_payload: BackendCardPaymentStatus,
    status: CardPaymentStatus,
) -> dict[str, Any]:
    """The structured facts one payment status reports.

    Three separate facts, kept separate on purpose, because collapsing any two of them is how
    a user gets told something untrue:

    * ``paid`` -- the platform says money arrived. It comes from the platform's own status
      word and from nothing else: never from a redirect, a returning user, or a claim made in
      the conversation. Only the signature-verified backend webhook can move a payment to
      paid, and this server only ever *reads* the result of that.
    * ``provisioned`` -- the platform says the eSIM exists. Reported as the platform reported
      it, and never inferred from ``paid``.
    * ``is_final`` -- no later check can change the answer.

    ``next_action`` is the platform's own instruction for retrieving the eSIM and is passed
    through verbatim when it sends one; it is never invented for a status that carries none.
    """
    stored = checkout.result or {}
    payload: dict[str, Any] = {
        "status": status.value.lower(),
        "payment_reference": checkout.payment_reference or "",
        "payment_status": status.value,
        "paid": status.money_arrived,
        "charged": status.money_arrived,
        "provisioned": bool(status_payload.provisioned),
        "is_final": status.is_terminal,
        "payment_method": "Card",
        "next_step": _STATUS_GUIDANCE[status],
        "proof_note": REDIRECT_IS_NOT_PROOF,
        "card_entry_note": CARD_ENTRY_NOTE,
    }
    if bundle := stored.get("bundle"):
        payload["bundle"] = bundle
    amount = status_payload.amount if status_payload.amount is not None else stored.get("amount")
    if amount is not None:
        payload["amount"] = str(amount)
    currency = status_payload.currency or stored.get("currency")
    if currency:
        payload["currency"] = currency
    if status is CardPaymentStatus.PENDING and stored.get("checkout_url"):
        # Re-offered rather than re-created: the same link the user already has.
        payload["checkout_url"] = stored["checkout_url"]
    if status_payload.expires_at:
        payload["expires_at"] = status_payload.expires_at
    if status_payload.order_id:
        payload["order_id"] = status_payload.order_id
    if status_payload.bundle_code:
        payload["bundle_code"] = status_payload.bundle_code
    if status_payload.quote_reference:
        payload["quote_reference"] = status_payload.quote_reference
    if status_payload.next_action:
        payload["next_action"] = status_payload.next_action
    if status in (CardPaymentStatus.FAILED, CardPaymentStatus.EXPIRED, CardPaymentStatus.CANCELLED):
        payload["new_checkout_required"] = True
    return payload


def _require_quote_reference(quote_reference: str) -> str:
    candidate = (quote_reference or "").strip()
    if not candidate:
        raise InvalidInputError(
            "A quote reference from a prepare_purchase result in this conversation is required before a payment "
            "page can be opened. Never invent one, and never start a payment the user has not agreed to."
        )
    return candidate


def register_card_checkout_tools(server: MCPServer, service: CardPaymentService) -> None:
    """Bind the Phase 5B tools onto an :class:`MCPServer` instance.

    Exactly two tools. There is no ``pay``, no ``capture``, no ``confirm_card_payment``, no
    refund, no top-up and no provisioning tool -- a model cannot call what does not exist, and
    this phase adds the ability to open one hosted payment page and to read its state, and
    nothing else.
    """

    @server.tool(
        name="create_card_checkout",
        title="Open a secure card payment page for a prepared eSIM plan",
        description=(
            "Open the eSIM platform's secure hosted checkout page for a plan the user prepared "
            "for card payment, and return the payment link. THIS CHARGES NOTHING BY ITSELF: it "
            "creates a payment page, and the user pays on that page or does not.\n"
            "WHEN: only after the user has been told the plan, the exact amount and the payment "
            'method, and has explicitly said yes to paying that amount by card -- "yes, pay by '
            'card", "open the payment page", "go ahead". Never call this on your own initiative, '
            "and never because the user merely asked to prepare or price a plan. Wanting a quote "
            "is not agreeing to pay.\n"
            "FIRST: the user must be signed in, and the plan must already be prepared with "
            "prepare_purchase for 'Card'. Read the amount back to them and wait for their "
            "answer. If the quote has expired, prepare it again and get their agreement to the "
            "new amount -- never start a payment for an amount they have not heard.\n"
            "Pass only the quote reference from your own prepare_purchase result. The plan, the "
            "price, the currency and the payment method come from that stored quote, so you "
            "cannot supply or change any of them here.\n"
            "NEVER ask the user for a card number, an expiry date, a security code, a cardholder "
            "name or any other card detail, and never offer to enter one for them. Card details "
            "are entered only on Stripe's own secure hosted page, which the returned link opens "
            "in the user's own browser. This server never sees a card.\n"
            "SAFE TO REPEAT: calling this twice for the same prepared quote returns the same "
            "payment link and never opens a second page, so the user is never asked to pay "
            "twice.\n"
            "AFTER SUCCESS: give the user the link, the plan and the amount, and say plainly "
            "that nothing has been charged yet. Then wait -- do not check the payment until "
            "they say they have paid or ask you to check, and use check_card_payment_status "
            "when they do."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def create_card_checkout(
        quote_reference: QuoteReferenceArg,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "create_card_checkout", lambda: service.create_card_checkout(quote_reference=quote_reference, ctx=ctx)
        )

    @server.tool(
        name="check_card_payment_status",
        title="Check what happened to a card payment",
        description=(
            "Ask the eSIM platform what happened to a card payment you started with "
            "create_card_checkout. This is the ONLY way to know whether a card payment went "
            "through: the platform's own payment webhook confirms the payment and sets up the "
            "eSIM, and this tool reads the result of that. Nothing here can make a payment "
            "succeed.\n"
            "WHEN: the user says they have paid, or asks you to check. Do not call it on a loop "
            "and do not keep checking on your own -- one check per request, and only when there "
            "is a reason to.\n"
            "NEVER treat any of these as proof of payment: the user returning to this "
            "conversation, a browser redirect, a success screen on the payment page, or the "
            "user simply saying it worked. Only this tool's answer counts, and there is no "
            "argument here through which you could tell it that a payment succeeded.\n"
            "The answer means exactly what it says:\n"
            "- not paid yet: give the user the link again and wait; do not ask for card details.\n"
            "- payment received: the money arrived but the eSIM is not ready -- never say the "
            "plan is active, installed or activated; offer to check again in a minute.\n"
            "- complete: the plan was paid for by card and the order is done. Tell the user "
            "plainly, with the plan name and the amount.\n"
            "- failed, expired or cancelled: nothing was charged. Say so plainly and offer to "
            "prepare the plan again and open a new payment page.\n"
            "IF THE RESULT IS UNCLEAR: if the payment is reported as unresolved or as needing "
            "support, never say it succeeded and never say it failed. Do not check it again, do "
            "not open another payment page and do not prepare another quote for the same plan "
            "-- tell the user the platform is investigating it and that they should contact "
            "eSIM support."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def check_card_payment_status(
        payment_reference: PaymentReferenceArg,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "check_card_payment_status",
            lambda: service.check_card_payment_status(payment_reference=payment_reference, ctx=ctx),
        )
