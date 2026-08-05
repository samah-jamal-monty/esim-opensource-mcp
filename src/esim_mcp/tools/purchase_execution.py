"""Phase 4 MCP tool: executing a wallet purchase the user has explicitly confirmed.

This is the first tool in this server that can spend a user's money, so the contract it
enforces is narrower than anything before it.

What ``confirm_purchase`` may take from the model
------------------------------------------------
**One argument: the reference of a quote this same caller prepared.** Nothing else. The
plan, the price, the currency, the payment method, the destination context and the user are
all read from the *stored quote*, so a hallucinated amount, a swapped bundle code or an
invented balance has no way into the request. There is no price argument to poison, and no
payment-method argument through which a card flow could be requested.

What it guarantees about money
------------------------------
* **One key per quote.** The ``Idempotency-Key`` is minted once, from
  :func:`~esim_mcp.purchase.execution.new_idempotency_key`, and reused for every later
  attempt on that quote. A retry -- transport level or model level -- therefore presents the
  platform with the key it already knows, which is what stops a second purchase.
* **A new key is never generated after an unclear answer.** A timeout, a dropped connection
  or an in-progress report leaves the execution unresolved and *keeps* its key; the recovery
  is to ask the platform again with that same key, never to start a fresh purchase.
* **Consumed only on success.** The quote is marked spent after the platform confirms a
  completed purchase, and at no other time.
* **No claim beyond the evidence.** Anything this server cannot resolve is reported as
  unknown, with instructions never to tell the user it either succeeded or failed.

What it never returns
---------------------
No access token, refresh token, idempotency key, key fingerprint, request hash, backend
correlation id, developer message, wallet transaction internal, activation code or ICCID.
The result carries the order id, the business status, the plan, the quoted amount and the
provisioning state -- the facts an assistant needs to tell a traveller what happened.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from esim_mcp.client.purchase import PurchaseApiClient
from esim_mcp.errors import (
    AuthenticationRequiredError,
    BackendTimeoutError,
    BackendUnavailableError,
    BundleUnavailableError,
    EsimMcpError,
    IdempotencyConflictError,
    InsufficientWalletBalanceError,
    InvalidInputError,
    NonWalletQuoteError,
    PurchaseCurrencyMismatchError,
    PurchaseInProgressError,
    PurchaseManualInterventionError,
    PurchaseOutcomeUnknownError,
    PurchaseRejectedError,
    PurchaseRouteUnavailableError,
    PurchaseUnavailableError,
    RateLimitedError,
    WalletUnavailableError,
)
from esim_mcp.models.purchase import BackendPurchaseResult, parse_purchase_result
from esim_mcp.purchase.execution import (
    ExecutionStatus,
    PurchaseExecution,
    PurchaseExecutionService,
    key_fingerprint,
    replay_error,
    require_attempts_remaining,
    require_confirmable_quote,
)
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

#: The sentence a successful result carries. Unlike its Phase 3 counterpart, this one says
#: that money moved -- and it must never appear on any branch where that is not established.
PURCHASE_COMPLETED = "The order was created and the wallet was charged."

#: What the assistant should do once a purchase is confirmed.
COMPLETED_NEXT_STEP = (
    "Tell the user plainly that the plan was bought and paid for from their wallet, and give them the plan name "
    "and the amount. Do not read the order reference out unless they ask for it. Installing or activating the "
    "eSIM is not something this version can do: point them to the eSIM app or their order confirmation email."
)

#: Repeated on a replayed result so the assistant does not describe one purchase as two.
REPLAY_NOTE = (
    "This purchase had already been completed and this is the stored result of it, not a second purchase. "
    "The user was charged once. Never tell them they bought it twice."
)

QuoteReferenceArg = Annotated[
    str,
    Field(
        description=(
            "The quote reference returned by prepare_purchase in this conversation, for the plan "
            "the user has just explicitly agreed to buy. Never invent, guess or edit one, and "
            "never take one from the user -- use the reference from your own prepare_purchase "
            "result. If you do not have one, or it has expired, prepare the plan again and ask "
            "the user to confirm the new amount before calling this."
        )
    ),
]


class PurchaseConfirmationService:
    """Backend-facing behaviour for ``confirm_purchase``."""

    def __init__(
        self,
        settings: Settings,
        purchase_client: PurchaseApiClient,
        session_manager: SessionManager,
        identity_provider: ClientIdentityProvider,
        quotes: PurchaseQuoteService,
        executions: PurchaseExecutionService,
    ) -> None:
        self._settings = settings
        self._purchases = purchase_client
        self._sessions = session_manager
        self._identity_provider = identity_provider
        self._quotes = quotes
        self._executions = executions

    # ------------------------------------------------------------------ internals

    async def _owner(self, ctx: Any | None) -> tuple[str, QuoteOwner]:
        """Resolve the caller and require a signed-in eSIM user.

        The owner key is built exactly as ``prepare_purchase`` builds it, from the verified
        MCP client identity plus a digest of the authenticated user id, so a quote prepared
        by one client-and-user pair can only ever be confirmed by that same pair.
        """
        identity = await self._identity_provider.resolve(ctx)
        device_id = derive_device_id(self._settings.salt_bytes(), identity)
        session = await self._sessions.require_session(identity.session_key)
        return device_id, QuoteOwner(session_key=identity.session_key, user_ref=user_ref_of(session))

    # ---------------------------------------------------------------------- tool

    async def confirm_purchase(self, *, quote_reference: str, ctx: Any | None = None) -> dict[str, Any]:
        """Buy the plan a prepared quote describes. Creates an order and may debit the wallet."""
        device_id, owner = await self._owner(ctx)
        reference = _require_quote_reference(quote_reference)

        # Held across the backend call on purpose: two confirmations of the same quote must
        # not both reach the platform, whatever order the client sends them in.
        async with self._executions.lock(reference):
            quote = await self._quotes.get_record(owner, reference)
            execution = await self._executions.acquire(owner, reference)

            replayed = self._replay_if_terminal(execution, quote)
            if replayed is not None:
                return replayed

            # Only reached when nothing terminal is recorded, so these gates can never reject
            # a purchase that already happened.
            require_usable_quote(quote)
            require_confirmable_quote(quote)
            require_attempts_remaining(execution)

            return await self._execute(quote=quote, execution=execution, owner=owner, device_id=device_id)

    def _replay_if_terminal(self, execution: PurchaseExecution, quote: PurchaseQuote) -> dict[str, Any] | None:
        """Hand back the stored answer for an execution that already reached one.

        Returning the *stored* result rather than asking the platform again is the whole
        point: a repeated confirmation must cost the user nothing and must not read as a
        second purchase.
        """
        if execution.status is ExecutionStatus.SUCCEEDED and execution.result is not None:
            logger.info("purchase_replayed_locally", extra={"quote_ref": quote_ref(quote.quote_id)})
            return {**execution.result, "replayed": True, "replay_note": REPLAY_NOTE}
        if execution.status.is_terminal:
            raise replay_error(execution)
        return None

    async def _execute(
        self,
        *,
        quote: PurchaseQuote,
        execution: PurchaseExecution,
        owner: QuoteOwner,
        device_id: str,
    ) -> dict[str, Any]:
        """Send exactly one purchase for ``quote`` and interpret the answer."""
        # Counted before the call, so an attempt that dies mid-flight is still an attempt.
        execution = await self._executions.record_attempt(execution)

        # Refreshed *before* the purchase rather than replayed after a 401: a replay would be
        # a second POST, and this call is not one to repeat on this server's own initiative.
        session = await self._sessions.ensure_fresh_session(
            owner.session_key,
            device_id=device_id,
            locale=quote.locale,
            currency=quote.price.currency,
        )

        logger.info(
            "purchase_execution_sending",
            extra={
                "quote_ref": quote_ref(quote.quote_id),
                "key_fp": key_fingerprint(execution.idempotency_key),
                "attempt": execution.attempts,
            },
        )

        try:
            outcome = await self._purchases.purchase_bundle_with_wallet(
                device_id=device_id,
                access_token=session.access_token,
                idempotency_key=execution.idempotency_key,
                bundle_code=quote.bundle.bundle_code,
                quote_reference=quote.quote_id,
                search_context=quote.search_context,
                locale=quote.locale,
                currency=quote.price.currency,
            )
        except (BackendTimeoutError, BackendUnavailableError):
            # The request may have been executed in full before the connection died. This is
            # the one branch where saying "it failed" would be a lie with a price attached.
            await self._executions.mark_unresolved(execution, PurchaseOutcomeUnknownError())
            logger.warning("purchase_outcome_unknown", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise PurchaseOutcomeUnknownError(details=_unknown_details(execution)) from None

        if outcome.is_success:
            return await self._on_success(outcome_data=outcome.data, quote=quote, execution=execution, owner=owner)

        raise await self._on_failure(
            outcome_status=outcome.effective_status,
            hint=outcome.error_hint,
            parsed=outcome.parsed,
            data=outcome.data,
            quote=quote,
            execution=execution,
        )

    async def _on_success(
        self,
        *,
        outcome_data: Any,
        quote: PurchaseQuote,
        execution: PurchaseExecution,
        owner: QuoteOwner,
    ) -> dict[str, Any]:
        """Interpret a successful envelope. Only a *completed* purchase counts as bought."""
        result = parse_purchase_result(outcome_data)
        if result is None or not result.is_completed:
            # A success envelope this server cannot read as a completed purchase is not a
            # failure: the wallet may well have been debited behind it.
            await self._executions.mark_unresolved(execution, PurchaseOutcomeUnknownError())
            logger.warning("purchase_success_envelope_unreadable", extra={"quote_ref": quote_ref(quote.quote_id)})
            raise PurchaseOutcomeUnknownError(details=_unknown_details(execution))

        payload = _completed_result(quote, result)
        await self._executions.mark_succeeded(execution, payload)
        # Consumed only here: after a terminal, confirmed, successful purchase.
        await self._quotes.consume(owner, quote.quote_id)
        logger.info(
            "purchase_completed",
            extra={
                "quote_ref": quote_ref(quote.quote_id),
                "key_fp": key_fingerprint(execution.idempotency_key),
                "order_created": True,
                "charged": True,
                "idempotent_replay": result.idempotent_replay,
            },
        )
        return {**payload, "replayed": bool(result.idempotent_replay)}

    async def _on_failure(
        self,
        *,
        outcome_status: int,
        hint: str,
        parsed: bool,
        data: Any,
        quote: PurchaseQuote,
        execution: PurchaseExecution,
    ) -> EsimMcpError:
        """Classify a non-successful answer, record it, and return the error to raise.

        The error is *returned* rather than raised here so that the recording above it can
        never be skipped by an early exit, and so the call site reads as the single place a
        purchase failure leaves this service.

        Three outcomes are possible and they are not interchangeable:

        * **terminal failure** -- the platform refused before spending anything. Recorded, so
          a repeated confirmation replays the refusal instead of asking again.
        * **escalation** -- the platform says the wallet may have been charged and it cannot
          resolve the purchase. Recorded as final; this quote is never sent again.
        * **unresolved** -- nobody can say yet. The key is kept and may be presented again.
        """
        error, disposition = _classify_failure(outcome_status, hint, parsed, data)

        if disposition is _Disposition.ESCALATE:
            await self._executions.mark_escalated(execution, error)
        elif disposition is _Disposition.TERMINAL:
            await self._executions.mark_failed(execution, error)
        elif disposition is _Disposition.UNRESOLVED:
            await self._executions.mark_unresolved(execution, error)
        # _Disposition.RETRYABLE leaves the record pending: nothing was spent, and the same
        # key stays reserved for the next attempt.

        logger.info(
            "purchase_not_completed",
            extra={
                "quote_ref": quote_ref(quote.quote_id),
                "key_fp": key_fingerprint(execution.idempotency_key),
                "http_status": outcome_status,
                "error_code": error.code,
                "disposition": disposition.value,
            },
        )
        return error


# ------------------------------------------------------------------ outcome classification


class _Disposition(StrEnum):
    """How an unsuccessful outcome is remembered.

    ``RETRYABLE`` deliberately records nothing: the platform refused before spending
    anything, so the execution stays pending and keeps its key for the next attempt.
    """

    ESCALATE = "escalate"
    TERMINAL = "terminal"
    UNRESOLVED = "unresolved"
    RETRYABLE = "retryable"


#: Backend error labels matched for internal classification only. The platform's envelope has
#: no machine-readable code: ``title`` is a localized string that falls back to the internal
#: error key when no translation exists, so both forms are matched here. Neither is ever
#: forwarded to an MCP client.
_BUSINESS_ERROR_MATCHERS: tuple[tuple[tuple[str, ...], type[EsimMcpError]], ...] = (
    (("insufficient_wallet_balance", "insufficient wallet"), InsufficientWalletBalanceError),
    (("wallet_not_found", "wallet not found"), WalletUnavailableError),
    (("bundle_not_available", "bundle not available"), BundleUnavailableError),
    (("unsupported_currency", "unsupported currency"), PurchaseCurrencyMismatchError),
    (("unsupported_payment_type", "unsupported payment"), NonWalletQuoteError),
)


def _classify_failure(
    status: int,
    hint: str,
    parsed: bool,
    data: Any,
) -> tuple[EsimMcpError, _Disposition]:
    """Map one unsuccessful purchase answer to a typed error and how to remember it."""
    lowered = (hint or "").strip().lower()
    order_id = _order_id_of(data)

    # 424 is the platform's dedicated "the wallet may have been charged without a usable
    # eSIM" status. It is deliberately distinct from 409 so a client can tell "escalate"
    # apart from "retry the same key later", and it must never be retried.
    if status == 424:
        return PurchaseManualInterventionError(details=_support_details(order_id)), _Disposition.ESCALATE

    if status in (401, 403):
        return AuthenticationRequiredError(
            "The eSIM session was refused by the platform, so nothing was ordered and nothing was charged. Ask "
            "the user to sign in again, then prepare the plan again and confirm the new amount with them."
        ), _Disposition.RETRYABLE

    if status == 404:
        return PurchaseRouteUnavailableError(), _Disposition.RETRYABLE

    if status == 429:
        return RateLimitedError(
            "The eSIM platform is rate limiting this client, so the purchase was not attempted and nothing was "
            "charged. Tell the user and offer to try the same purchase again shortly."
        ), _Disposition.RETRYABLE

    if status == 409:
        # "Still processing" is not a conflict: the same key is already doing exactly what
        # this call wanted, so the answer is to wait and ask again with it, never to re-key.
        if "in_progress" in lowered or "in progress" in lowered or "still processing" in lowered:
            return PurchaseInProgressError(), _Disposition.UNRESOLVED
        return IdempotencyConflictError(), _Disposition.TERMINAL

    if status in (400, 422):
        for needles, error_type in _BUSINESS_ERROR_MATCHERS:
            if any(needle in lowered for needle in needles):
                return error_type(), _Disposition.TERMINAL
        return PurchaseRejectedError(), _Disposition.TERMINAL

    if status == 503:
        return PurchaseUnavailableError(), _Disposition.RETRYABLE

    if not parsed and 200 <= status < 300:
        return PurchaseOutcomeUnknownError(), _Disposition.UNRESOLVED

    if status >= 500:
        # 502/504 and friends: the request may well have reached the platform and run.
        return PurchaseOutcomeUnknownError(), _Disposition.UNRESOLVED

    # An unexpected 4xx the platform did not explain. It refused, so nothing was spent.
    return PurchaseRejectedError(), _Disposition.TERMINAL


def _order_id_of(data: Any) -> str | None:
    result: BackendPurchaseResult | None = parse_purchase_result(data)
    return result.order_id if result is not None else None


def _support_details(order_id: str | None) -> dict[str, Any]:
    """Safe escalation payload. An order reference only; never a key, hash or trace."""
    details: dict[str, Any] = {"retry_safe": False, "next_step": "contact_support"}
    if order_id:
        details["order_id"] = order_id
    return details


def _unknown_details(execution: PurchaseExecution) -> dict[str, Any]:
    """Safe payload for an outcome this server never learned."""
    return {
        "retry_safe": False,
        "new_purchase_safe": False,
        "next_step": "check_same_purchase_again",
        "attempts_remaining": execution.attempts_remaining,
    }


# ------------------------------------------------------------------------ result shaping


def _completed_result(quote: PurchaseQuote, result: BackendPurchaseResult) -> dict[str, Any]:
    """The structured facts a completed purchase reports.

    ``order_created`` and ``charged`` are ``True`` here and nowhere else in this codebase --
    they are only ever set on the one branch where the platform confirmed a completed
    purchase, which is what makes them trustworthy to read out.
    """
    payload: dict[str, Any] = {
        "status": "purchased",
        "quote_reference": quote.quote_id,
        "order_created": True,
        "charged": True,
        "payment_method": quote.payment_method.value,
        "bundle": {
            "bundle_code": quote.bundle.bundle_code,
            "name": quote.bundle.name,
            "data": quote.bundle.data_display,
            "validity": quote.bundle.validity_display,
        },
        "pricing": {
            "quoted_amount": money_text(quote.price.displayed_amount),
            "currency": quote.price.currency,
        },
        "message": PURCHASE_COMPLETED,
        "next_step": COMPLETED_NEXT_STEP,
    }
    if result.order_id:
        payload["order_id"] = result.order_id
    if result.order_status:
        payload["order_status"] = result.order_status
    if result.payment_status:
        payload["payment_status"] = result.payment_status
    if result.provisioning_status:
        payload["provisioning_status"] = result.provisioning_status
    if result.next_action:
        payload["next_state"] = result.next_action
    if quote.bundle.activation_policy:
        payload["bundle"]["activation_policy"] = quote.bundle.activation_policy
    payload["price_note"] = (
        "This is the amount the quote showed the user. The platform settles the final amount itself, so present "
        "it as what the plan cost rather than as a receipt total."
    )
    return payload


def _require_quote_reference(quote_reference: str) -> str:
    candidate = (quote_reference or "").strip()
    if not candidate:
        raise InvalidInputError(
            "A quote reference from a prepare_purchase result in this conversation is required before anything "
            "can be bought. Never invent one, and never buy a plan the user has not confirmed."
        )
    return candidate


def register_purchase_execution_tools(server: MCPServer, service: PurchaseConfirmationService) -> None:
    """Bind the Phase 4 tool onto an :class:`MCPServer` instance.

    Exactly one tool. There is no ``pay``, no ``checkout``, no card flow, no top-up, no
    voucher and no provisioning tool -- a model cannot call what does not exist, and this
    phase adds the ability to spend a wallet balance and nothing else.
    """

    @server.tool(
        name="confirm_purchase",
        title="Buy a prepared eSIM plan with the user's wallet",
        description=(
            "Buy the plan in a quote you prepared earlier, paying from the user's eSIM wallet. "
            "THIS SPENDS REAL MONEY: it creates an order at the eSIM platform and debits the "
            "wallet balance. It cannot be undone from here, and there is no refund tool.\n"
            "WHEN: only after the user has been told the plan, the exact amount and the "
            'payment method, and has explicitly said yes to that amount -- "yes, buy it", '
            '"confirm", "go ahead". Never call this on your own initiative, never to '
            '"check" something, and never because the user merely asked to prepare or price '
            "a plan. Wanting a quote is not agreeing to be charged.\n"
            "FIRST: prepare the plan with prepare_purchase, read the amount back to the user, "
            "and wait for their answer. If the quote has expired, prepare it again and get "
            "their agreement to the new amount -- never confirm an amount they have not heard.\n"
            "Pass only the quote reference from your own prepare_purchase result. The plan, "
            "the price, the currency and the payment method come from that stored quote, so "
            "you cannot supply or change any of them here.\n"
            "Wallet only in this version. A quote prepared for card payment cannot be bought.\n"
            "SAFE TO REPEAT: calling this twice with the same quote reference returns the "
            "stored result of the first purchase and never buys the plan twice.\n"
            "AFTER SUCCESS: tell the user plainly that the plan was bought and paid for from "
            "their wallet, with the plan name and the amount.\n"
            "IF THE RESULT IS UNCLEAR: if the outcome is reported as unknown or as needing "
            "support, never say the purchase succeeded and never say it failed. Do not prepare "
            "a new quote for the same plan and do not try to buy it again -- say the platform "
            "is confirming it, and offer to check the same purchase again."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True),
    )
    async def confirm_purchase(
        quote_reference: QuoteReferenceArg,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "confirm_purchase", lambda: service.confirm_purchase(quote_reference=quote_reference, ctx=ctx)
        )
