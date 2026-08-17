"""Typed application errors with MCP-safe messages.

Every error carries a stable machine-readable ``code`` and a message that is safe to
hand to an MCP client. Backend ``developerMessage`` payloads, stack traces, provider
responses, JWTs, refresh tokens and OTPs never reach these messages.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AccountReadTimeoutError",
    "AmbiguousCountryError",
    "AmbiguousRegionError",
    "AuthenticationRequiredError",
    "BackendTimeoutError",
    "BackendUnavailableError",
    "BundleNotFoundError",
    "BundleUnavailableError",
    "CardCheckoutAttemptLimitError",
    "CardCheckoutOutcomeUnknownError",
    "CardCheckoutRejectedError",
    "CardCheckoutUnavailableError",
    "CardPaymentAmbiguousError",
    "CardPaymentCheckLimitError",
    "CardPaymentNotFoundError",
    "CardPaymentStatusUnavailableError",
    "CatalogUnavailableError",
    "ConsumptionUnavailableError",
    "CountryNotFoundError",
    "EsimMcpError",
    "EsimNotFoundError",
    "EsimSelectionOutOfRangeError",
    "EsimSelectionUnavailableError",
    "EsimTopupAlreadyAttemptedError",
    "EsimTopupConfirmationRequiredError",
    "EsimTopupExecutionUnavailableError",
    "EsimTopupNotSupportedError",
    "EsimTopupOptionsUnavailableError",
    "EsimTopupOutcomeUnknownError",
    "EsimTopupRejectedError",
    "ExpiredOtpError",
    "ForbiddenBackendRouteError",
    "IdempotencyConflictError",
    "IdentityUnavailableError",
    "InsufficientWalletBalanceError",
    "InternalError",
    "InvalidBackendResponseError",
    "InvalidInputError",
    "InvalidOtpError",
    "NoConsumptionDataError",
    "NoMatchingBundlesError",
    "NoPurchasedEsimsError",
    "NoTopupOptionsError",
    "NonCardQuoteError",
    "NonWalletQuoteError",
    "OtpLimitReachedError",
    "OtpStillActiveError",
    "OtpTooFrequentError",
    "PurchaseAttemptLimitError",
    "PurchaseCurrencyMismatchError",
    "PurchaseInProgressError",
    "PurchaseManualInterventionError",
    "PurchaseOutcomeUnknownError",
    "PurchaseRejectedError",
    "PurchaseRouteUnavailableError",
    "PurchaseUnavailableError",
    "QuoteCancelledError",
    "QuoteConsumedError",
    "QuoteExpiredError",
    "QuoteNotFoundError",
    "QuoteNotOwnedError",
    "RateLimitedError",
    "RegionNotFoundError",
    "TooManyActiveQuotesError",
    "TopupBundleIncompatibleError",
    "TopupQuoteCancelledError",
    "TopupQuoteExpiredError",
    "TopupQuoteNotFoundError",
    "UnsafeCheckoutLinkError",
    "UnsupportedPaymentMethodError",
    "WalletTopupAmountInvalidError",
    "WalletTopupLimitReachedError",
    "WalletTopupNotFoundError",
    "WalletTopupOutcomeUnknownError",
    "WalletTopupQuoteNotFoundError",
    "WalletTopupRejectedError",
    "WalletTopupStatusUnavailableError",
    "WalletTopupUnavailableError",
    "WalletUnavailableError",
]


class EsimMcpError(Exception):
    """Base class for every error surfaced to MCP clients.

    ``str(error)`` is deliberately the safe message: the MCP SDK renders uncaught tool
    exceptions with ``str(exc)``, so the safe message is the only thing that can leak.
    """

    code: str = "internal_error"
    default_message: str = "The request could not be completed."
    retryable: bool = False

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None) -> None:
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)

    def __str__(self) -> str:
        # The SDK renders an uncaught tool exception with ``str(exc)``; keep it safe and
        # machine-readable at the same time.
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        """Safe, structured representation for tool results."""
        payload: dict[str, Any] = {"status": "error", "error_code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class AuthenticationRequiredError(EsimMcpError):
    code = "authentication_required"
    default_message = "No active eSIM session. Request an OTP and verify it before calling this tool."


class IdentityUnavailableError(EsimMcpError):
    """Raised when no verified MCP client identity can be resolved (fail-closed)."""

    code = "client_identity_unavailable"
    default_message = (
        "This MCP client could not be identified from an authenticated transport, so no isolated session can be used."
    )


class InvalidOtpError(EsimMcpError):
    code = "invalid_otp"
    default_message = "The verification code is invalid."


class ExpiredOtpError(EsimMcpError):
    code = "expired_otp"
    default_message = "The verification code has expired. Request a new one."


class OtpStillActiveError(EsimMcpError):
    code = "otp_still_active"
    default_message = "A verification code is still active. Use it or wait for it to expire."


class OtpLimitReachedError(EsimMcpError):
    code = "otp_limit_reached"
    default_message = "The maximum number of verification-code requests has been reached. Try again later."


class OtpTooFrequentError(EsimMcpError):
    code = "otp_request_too_frequent"
    default_message = "Verification codes are being requested too frequently. Wait before trying again."


class InvalidInputError(EsimMcpError):
    code = "invalid_input"
    default_message = "The supplied arguments are invalid."


class RateLimitedError(EsimMcpError):
    code = "rate_limited"
    default_message = "The eSIM backend is rate limiting this client. Try again later."


class BackendUnavailableError(EsimMcpError):
    code = "backend_unavailable"
    default_message = "The eSIM backend is currently unavailable."
    retryable = True


class BackendTimeoutError(EsimMcpError):
    code = "backend_timeout"
    default_message = "The eSIM backend did not respond in time."
    retryable = True


class AccountReadTimeoutError(BackendTimeoutError):
    """One of the two account-history reads ran past its own read budget.

    A subclass of :class:`BackendTimeoutError` so every existing handler still catches it,
    and a distinct type so the one thing that must never happen to this failure -- being
    reported as an empty account -- can be ruled out in words the model actually receives.

    The message is deliberately an instruction not to retry. Nothing was created and nothing
    was charged, so a repeat is *safe*; it is simply useless. The platform is still building
    the same answer, and a second attempt at this budget only doubles the silence the user
    sits through before hearing anything at all. The user asks again when they want to.
    """

    code = "account_read_timeout"
    default_message = (
        "The eSIM platform did not finish reading this account in time, so nothing could be read. "
        "This is a slow platform, NOT an empty account: never tell the user they have no eSIMs, no "
        "plans and no orders on the strength of this, and never say an eSIM they bought is missing. "
        "Do NOT call the tool again by yourself -- trying again straight away only makes the user "
        "wait twice. Tell them the platform is being slow right now and that they can ask again in a "
        "few minutes."
    )
    retryable = True


class InvalidBackendResponseError(EsimMcpError):
    code = "invalid_backend_response"
    default_message = "The eSIM backend returned an unexpected response."


class InternalError(EsimMcpError):
    code = "internal_error"
    default_message = "An internal error occurred while handling the request."


# --------------------------------------------------------------------------- catalogue
#
# Catalogue errors carry their next action *inside the message*: the MCP SDK renders an
# uncaught tool exception with ``str(exc)``, so a structured ``details`` payload would not
# reach the model. Suggestions and choices are therefore part of the sentence.


class CatalogUnavailableError(BackendUnavailableError):
    """The catalogue could not be read. A subclass so existing handling still applies."""

    code = "catalog_unavailable"
    default_message = (
        "The eSIM plan catalogue is temporarily unavailable. Tell the user and offer to try again shortly."
    )


class CountryNotFoundError(EsimMcpError):
    code = "country_not_found"
    default_message = "That country is not available in the eSIM catalogue. Ask the user for another destination."


class AmbiguousCountryError(EsimMcpError):
    code = "ambiguous_country"
    default_message = "Several countries match that name. Ask the user which one they mean."


class RegionNotFoundError(EsimMcpError):
    code = "region_not_found"
    default_message = "That region is not available in the eSIM catalogue. Ask the user for another destination."


class AmbiguousRegionError(EsimMcpError):
    code = "ambiguous_region"
    default_message = "Several regions match that name. Ask the user which one they mean."


class BundleNotFoundError(EsimMcpError):
    code = "bundle_not_found"
    default_message = (
        "No plan with that code exists in the catalogue. Use a bundle_code from a result you have already "
        "shown the user; never invent one."
    )


class NoMatchingBundlesError(EsimMcpError):
    code = "no_matching_bundles"
    default_message = "No plans match those filters. Ask the user whether to relax them."


# ------------------------------------------------------------------ purchase preparation
#
# Phase 3 errors. Like the catalogue errors above, each message carries its own next action
# because the SDK renders an uncaught tool exception as ``str(exc)``.
#
# None of these mean money moved: no error in this group can be raised after a charge,
# because nothing in this phase can charge.


class BundleUnavailableError(EsimMcpError):
    code = "bundle_unavailable"
    default_message = (
        "The platform has stopped offering that plan, so it cannot be prepared. Tell the user it is no longer "
        "available and offer to search that destination again."
    )


class WalletUnavailableError(EsimMcpError):
    code = "wallet_unavailable"
    default_message = (
        "This account's wallet balance could not be read, so a wallet quote cannot be prepared. Tell the user "
        "and offer to prepare the plan for card payment instead."
    )


class InsufficientWalletBalanceError(EsimMcpError):
    code = "insufficient_wallet_balance"
    default_message = (
        "The wallet balance does not cover this plan. Tell the user the shortfall and offer either adding funds "
        "outside this assistant or paying by card instead."
    )


class UnsupportedPaymentMethodError(EsimMcpError):
    code = "unsupported_payment_method"
    default_message = "Only 'Wallet' and 'Card' can be prepared. Ask the user which of those two they want to use."


class QuoteNotFoundError(EsimMcpError):
    """Also returned for a quote owned by somebody else -- see :mod:`esim_mcp.purchase.store`."""

    code = "quote_not_found"
    default_message = (
        "No prepared quote with that reference exists for this client. Prepare the plan again before referring "
        "to a quote."
    )


class QuoteExpiredError(EsimMcpError):
    code = "quote_expired"
    default_message = (
        "That prepared quote has expired, so its price and balance are no longer current. Prepare the plan "
        "again if the user still wants it. Nothing was ordered and nothing was charged."
    )


class QuoteCancelledError(EsimMcpError):
    code = "quote_cancelled"
    default_message = (
        "That prepared quote was already cancelled. Prepare the plan again if the user still wants it. Nothing "
        "was ordered and nothing was charged."
    )


class QuoteConsumedError(EsimMcpError):
    code = "quote_consumed"
    default_message = "That prepared quote has already been used and cannot be prepared or read again."


class QuoteNotOwnedError(EsimMcpError):
    """Defence in depth only.

    Ownership is enforced by scoping every store lookup to the caller, so a foreign quote is
    invisible rather than refused (:class:`QuoteNotFoundError`) and no caller can use this
    server to discover that somebody else's quote exists. This error exists for the case
    where a store implementation returns a record whose owner does not match the caller --
    a bug, not a reachable request.
    """

    code = "quote_not_owned"
    default_message = "That prepared quote does not belong to this client."


class TooManyActiveQuotesError(EsimMcpError):
    code = "too_many_active_quotes"
    default_message = (
        "This client already has the maximum number of prepared quotes. Cancel one the user no longer wants, "
        "or wait for it to expire, before preparing another."
    )


# ------------------------------------------------------------------- purchase execution
#
# Phase 4 errors, raised by ``confirm_purchase`` only. Unlike every group above, an error
# here *may* be raised after money moved -- which is precisely why the ambiguous outcomes get
# their own types instead of collapsing into "backend unavailable". The rule each message has
# to carry is the same one the backend enforces: never start a second purchase, and never
# invent a new idempotency key for one the platform may already have executed.


class NonWalletQuoteError(EsimMcpError):
    code = "unsupported_quote_payment_method"
    default_message = (
        "Only a Wallet quote can be bought with confirm_purchase, and this quote was prepared for card payment. "
        "A card quote is paid on the platform's hosted checkout page instead: once the user has agreed to the "
        "amount, start that with create_card_checkout. Never take card details here."
    )


class PurchaseRejectedError(EsimMcpError):
    """The platform refused the purchase before executing it. Nothing was charged."""

    code = "purchase_rejected"
    default_message = (
        "The eSIM platform rejected this purchase and nothing was charged. Tell the user it did not go through "
        "and offer to prepare the plan again."
    )


class PurchaseCurrencyMismatchError(EsimMcpError):
    code = "purchase_currency_mismatch"
    default_message = (
        "The platform does not settle purchases in the currency this quote was priced in, so it was refused and "
        "nothing was charged. Prepare the plan again in the platform's own currency."
    )


class PurchaseRouteUnavailableError(EsimMcpError):
    code = "purchase_endpoint_unavailable"
    default_message = (
        "This eSIM platform does not offer the assisted purchase endpoint, so the purchase was not attempted and "
        "nothing was charged. Tell the user purchasing is unavailable here and offer to keep the quote."
    )


class PurchaseUnavailableError(EsimMcpError):
    """Purchase is switched off or temporarily unavailable. Nothing was charged."""

    code = "purchase_unavailable"
    default_message = (
        "Assisted purchasing is currently unavailable at the eSIM platform, so nothing was ordered and nothing "
        "was charged. Tell the user and offer to try again shortly."
    )
    retryable = True


class PurchaseInProgressError(EsimMcpError):
    """The platform is still executing this very purchase. Never start a second one."""

    code = "purchase_in_progress"
    default_message = (
        "This purchase is already being processed by the eSIM platform. Do NOT confirm again straight away and do "
        "NOT prepare a second quote for the same plan -- that would risk buying it twice. Tell the user it is "
        "still going through and offer to check again in a minute."
    )


class IdempotencyConflictError(EsimMcpError):
    """Same key, different purchase: only reachable through a bug, never through a retry."""

    code = "purchase_idempotency_conflict"
    default_message = (
        "The eSIM platform could not match this confirmation to the prepared quote, so nothing was ordered and "
        "nothing was charged. Prepare the plan again and confirm the new quote."
    )


class PurchaseManualInterventionError(EsimMcpError):
    """The backend reports the wallet may have been debited without a usable eSIM."""

    code = "purchase_needs_support"
    default_message = (
        "The purchase could not be completed cleanly and the wallet MAY already have been charged. Do NOT retry "
        "this purchase, do NOT prepare another quote for the same plan, and never tell the user it succeeded or "
        "failed. Tell them the platform is checking it and that they should contact eSIM support."
    )


class PurchaseOutcomeUnknownError(EsimMcpError):
    """The platform never gave a usable answer, so the outcome is genuinely unknown."""

    code = "purchase_outcome_unknown"
    default_message = (
        "The eSIM platform did not confirm the outcome of this purchase, so it may or may not have gone through "
        "and the wallet may or may not have been charged. Never say it succeeded and never say it failed. Do NOT "
        "prepare a new quote for the same plan. Tell the user the result is being confirmed and offer to check "
        "the same purchase again shortly."
    )


class PurchaseAttemptLimitError(EsimMcpError):
    code = "purchase_attempt_limit_reached"
    default_message = (
        "This purchase has been attempted the maximum number of times without a clear result. Do NOT try again "
        "and do NOT prepare another quote for the same plan. Tell the user to contact eSIM support so the "
        "platform can confirm whether it went through."
    )


# ------------------------------------------------------------------------ card checkout
#
# Phase 5B errors, raised by ``create_card_checkout`` and ``check_card_payment_status`` only.
#
# The money model here is the opposite way round from the wallet phase: **creating a checkout
# never charges anybody** -- the user pays on the payment provider's own hosted page, or they
# do not -- so a failure to create one is safe to describe as a failure. What is *not* safe is
# to describe a payment as taken, because this server never sees the card and never handles
# the payment: only a status read from the platform can say what happened.


class NonCardQuoteError(EsimMcpError):
    code = "unsupported_quote_payment_method_for_card"
    default_message = (
        "That quote was prepared for wallet payment, so it cannot be paid by card. Ask the user which they want: "
        "confirm_purchase pays from their wallet, or prepare the same plan again for card payment."
    )


class CardCheckoutUnavailableError(EsimMcpError):
    """Card checkout is switched off, or the platform does not offer the route."""

    code = "card_checkout_unavailable"
    default_message = (
        "Card checkout is currently unavailable at the eSIM platform, so no payment page was created and nothing "
        "was charged. Tell the user and offer either to try again shortly or to pay from their wallet instead."
    )
    retryable = True


class CardCheckoutRejectedError(EsimMcpError):
    """The platform refused to create a checkout page. Nothing was charged."""

    code = "card_checkout_rejected"
    default_message = (
        "The eSIM platform would not open a card payment page for this quote, and nothing was charged. Tell the "
        "user it did not start and offer to prepare the plan again."
    )


class CardCheckoutOutcomeUnknownError(EsimMcpError):
    """This server never learned whether a checkout page was created.

    Distinctly *less* severe than its wallet counterpart, and the message has to say so:
    creating a checkout page moves no money, so the honest thing to tell the user is that the
    payment page could not be opened -- never that a payment may have been taken.
    """

    code = "card_checkout_outcome_unknown"
    default_message = (
        "The answer to the card checkout request never arrived, so this server has no payment link to hand over. "
        "Nothing has been charged -- a payment page is not a payment. This is a lost answer, not a refusal: the "
        "platform may well have created the page. Do NOT prepare a new quote for the same plan. Tell the user the "
        "link did not come back this time and offer to try the SAME prepared plan again right away, which is what "
        "retrieves a link."
    )
    retryable = True


class CardCheckoutAttemptLimitError(EsimMcpError):
    code = "card_checkout_attempt_limit_reached"
    default_message = (
        "Opening a card payment page for this quote has been attempted the maximum number of times without a "
        "clear result. Do NOT try again. Nothing was charged: tell the user, and offer to pay from their wallet "
        "instead or to contact eSIM support."
    )


class UnsafeCheckoutLinkError(EsimMcpError):
    """The platform returned a payment link this server refuses to pass on.

    A payment link is the one value here a user is asked to *act on*, so an address that is
    not a plain ``https`` URL is never shown -- showing one would be this phase's worst
    possible failure, and a malformed link is far more likely to be a bug than a bargain.
    """

    code = "unsafe_checkout_link"
    default_message = (
        "The eSIM platform returned a payment address this server will not pass on, so no link was shown and "
        "nothing was charged. Never show the user a payment link you did not receive from a tool result. Tell "
        "them card payment could not be started and offer to pay from their wallet instead."
    )


class CardPaymentNotFoundError(EsimMcpError):
    """Also returned for a payment belonging to somebody else -- ownership is invisible."""

    code = "card_payment_not_found"
    default_message = (
        "No card payment with that reference was started by this client. Use the reference from your own "
        "create_card_checkout result, and never take one from the user or invent one."
    )


class CardPaymentStatusUnavailableError(EsimMcpError):
    code = "card_payment_status_unavailable"
    default_message = (
        "The eSIM platform could not report the state of this card payment just now. Never guess whether it went "
        "through: tell the user it could not be checked and offer to check the same payment again shortly."
    )
    retryable = True


class CardPaymentAmbiguousError(EsimMcpError):
    """The platform itself cannot resolve the payment. Stop, and never re-check on a loop."""

    code = "card_payment_ambiguous"
    default_message = (
        "The eSIM platform cannot resolve this card payment, and the card MAY already have been charged. Do NOT "
        "check it again, do NOT start another checkout and do NOT prepare another quote for the same plan. Never "
        "tell the user it succeeded or failed -- tell them the platform is investigating it and that they should "
        "contact eSIM support."
    )


class CardPaymentCheckLimitError(EsimMcpError):
    code = "card_payment_check_limit_reached"
    default_message = (
        "This card payment has been checked the maximum number of times without reaching a final state. Stop "
        "checking it. Tell the user the platform has not settled it yet and that they should check their eSIM "
        "app or contact eSIM support."
    )


# ---------------------------------------------------------------- purchased eSIM reads
#
# Phase 6A errors, raised by ``get_esim_consumption`` and by the top-up option read. None of
# them can be raised after a charge, because neither tool can charge: both are reads.


class NoPurchasedEsimsError(EsimMcpError):
    code = "no_purchased_esims"
    default_message = (
        "This account owns no eSIMs, so there is nothing to report usage for. Tell the user plainly that they "
        "have no eSIMs on this account yet, and offer to help them choose a plan. Never invent an eSIM."
    )


class EsimNotFoundError(EsimMcpError):
    """Also returned for an eSIM belonging to somebody else -- ownership is invisible.

    A caller must not be able to learn that an ICCID exists by being told "that is not
    yours", so an unknown ICCID and another user's ICCID give the identical answer.
    """

    code = "esim_not_found"
    default_message = (
        "No eSIM with that identifier is on this account. Use get_my_esims to see the eSIMs the signed-in user "
        "actually owns, and pick from those -- never invent an identifier and never take one from the user."
    )


class EsimSelectionUnavailableError(EsimMcpError):
    """A number was given, but this client-and-user has no numbered listing to resolve it in.

    Reached whenever the mapping from "number 2" to an ICCID cannot be trusted: nothing has
    been listed yet, the session was signed out or replaced, the client reconnected onto a
    fresh session, the server restarted, or the same client is now signed in as somebody else.
    Every one of those has the same honest answer -- list the eSIMs again -- because a number
    without the listing it came from does not identify anything.

    Deliberately *not* an invalid-input error. The caller did nothing wrong; the context it
    was relying on is gone, and the fix is a tool call rather than a corrected argument.
    """

    code = "esim_selection_unavailable"
    default_message = (
        "There is no current list of this user's eSIMs to resolve that number against, so it is not clear which "
        "eSIM was meant. This usually means nothing has been listed yet in this session, or the user signed out, "
        "signed in again, or reconnected since the last list. Call get_my_esims to show the numbered list again, "
        "then use a number from THAT list. Never guess which eSIM was meant and never carry a number over from an "
        "earlier list."
    )


class EsimSelectionOutOfRangeError(InvalidInputError):
    """The number is not one of the numbers in the caller's current listing.

    A subclass of :class:`InvalidInputError` because that is what it is -- a bad argument,
    caught before any backend call -- while still carrying its own code so the caller can tell
    "you picked 9 of 3" apart from every other rejected argument.
    """

    code = "esim_selection_out_of_range"
    default_message = (
        "That number is not on the current list of this user's eSIMs. Show the numbered list again and ask the "
        "user which one they mean, then use a number from that list. Never guess."
    )


class NoConsumptionDataError(EsimMcpError):
    """The platform answered, and had nothing to report. Not an error, and not zero usage."""

    code = "no_consumption_data"
    default_message = (
        "The platform has no usage figures for this eSIM yet. That usually means the plan has not started. Tell "
        "the user no usage has been reported yet -- never say they have used nothing, never say they have their "
        "full allowance left, and never work a figure out from a date."
    )


class ConsumptionUnavailableError(EsimMcpError):
    code = "consumption_unavailable"
    default_message = (
        "The eSIM platform could not report usage for this eSIM just now. Tell the user the figures could not be "
        "read and offer to check again shortly. Never guess how much data is left."
    )
    retryable = True


# ------------------------------------------------------------------------ eSIM top-up
#
# Phase 6B errors. The read and the preparation cannot charge; the execution tool that could
# is deliberately not implemented -- see :class:`EsimTopupExecutionUnavailableError`.


class EsimTopupNotSupportedError(EsimMcpError):
    code = "esim_topup_not_supported"
    default_message = (
        "The platform does not allow this eSIM to be topped up. Tell the user plainly and offer to look at a new "
        "plan for their destination instead."
    )


class NoTopupOptionsError(EsimMcpError):
    code = "no_topup_options"
    default_message = (
        "The platform offers no top-up plans for this eSIM. Say so plainly -- never offer a plan from the general "
        "catalogue as a top-up, because only the platform can say what is compatible with a SIM already in use."
    )


class EsimTopupOptionsUnavailableError(EsimMcpError):
    code = "esim_topup_options_unavailable"
    default_message = (
        "The eSIM platform could not list top-up plans for this eSIM just now. Tell the user and offer to try "
        "again shortly. Never present catalogue plans as if they were compatible top-ups."
    )
    retryable = True


class TopupBundleIncompatibleError(EsimMcpError):
    code = "topup_bundle_incompatible"
    default_message = (
        "That plan is not one the platform offers as a top-up for this eSIM. Use get_esim_topup_options and pick "
        "a plan from what it returns -- never a bundle_code from the general catalogue."
    )


class TopupQuoteNotFoundError(EsimMcpError):
    """Also returned for a quote owned by somebody else -- ownership is invisible."""

    code = "topup_quote_not_found"
    default_message = (
        "No prepared top-up with that reference exists for this client. Prepare the top-up again before referring "
        "to it."
    )


class TopupQuoteExpiredError(EsimMcpError):
    code = "topup_quote_expired"
    default_message = (
        "That prepared top-up has expired, so its price is no longer current. Prepare it again if the user still "
        "wants it. Nothing was ordered and nothing was charged."
    )


class TopupQuoteCancelledError(EsimMcpError):
    code = "topup_quote_cancelled"
    default_message = (
        "That prepared top-up was already cancelled. Prepare it again if the user still wants it. Nothing was "
        "ordered and nothing was charged."
    )


class EsimTopupExecutionUnavailableError(EsimMcpError):
    """Executing an eSIM top-up is switched off on this deployment.

    The platform's own top-up endpoint takes no idempotency key and keeps no record that a
    retry could be recognized from: a ``Topup`` order row carries no ICCID column and no
    request key, so two identical requests are indistinguishable from one request sent
    twice. Making a retry safe would need a durable key, which needs a schema change.

    Because of that the capability is behind a QA-only flag which defaults to off and which
    production settings refuse to construct. This error is what a caller gets when the flag
    is off: the tool is not registered at all in that state, so reaching this is either a
    direct service call or a deployment that turned the flag off mid-session.
    """

    code = "esim_topup_execution_unavailable"
    default_message = (
        "This assistant cannot carry out an eSIM top-up on this deployment: the platform cannot guarantee that a "
        "repeated request would not top the SIM up twice, so the step is switched off rather than risked. Tell "
        "the user the top-up has to be completed in the eSIM app or on the website, and give them the plan and "
        "the amount they were quoted so they can find it. Nothing was ordered and nothing was charged."
    )


class EsimTopupConfirmationRequiredError(EsimMcpError):
    """The final confirmation did not match the quote the user was shown."""

    code = "esim_topup_confirmation_required"
    default_message = (
        "This top-up was not carried out because the confirmation did not match the prepared quote. Read the "
        "eSIM, the plan and the exact amount back to the user from the prepared quote, get their explicit "
        "agreement to that amount, and confirm again with the amount exactly as the quote states it. Nothing "
        "was ordered and nothing was charged."
    )


class EsimTopupRejectedError(EsimMcpError):
    """The platform refused the top-up before executing it. Nothing was charged."""

    code = "esim_topup_rejected"
    default_message = (
        "The eSIM platform rejected this top-up and nothing was charged. Tell the user it did not go through "
        "and offer to prepare it again."
    )


class EsimTopupOutcomeUnknownError(EsimMcpError):
    """The platform never gave a usable answer, so the outcome is genuinely unknown.

    Terminal here in a way it is not for any other flow in this codebase. The wallet
    purchase and the two hosted checkouts may all be asked again with the key they already
    used; this route has no key, so asking again is a *second top-up*, not a question about
    the first. The quote is locked and never re-sent.
    """

    code = "esim_topup_outcome_unknown"
    default_message = (
        "The eSIM platform did not confirm the outcome of this top-up, so it may or may not have gone through "
        "and the wallet may or may not have been charged. Never say it succeeded and never say it failed. Do "
        "NOT try again and do NOT prepare another top-up for the same plan -- this step cannot be repeated "
        "safely, so a second attempt could charge the user twice. Tell the user to check their eSIMs, their "
        "data usage, their wallet balance or their order history to see whether it went through, and to contact "
        "eSIM support if it is still unclear."
    )


class EsimTopupAlreadyAttemptedError(EsimMcpError):
    """This quote has already been sent for execution. It may never be sent again.

    The lock that makes a non-idempotent write survivable: one quote, one attempt, whatever
    the outcome was. A caller that wants to try again has to prepare a fresh quote, which is
    a deliberate act rather than a retry.
    """

    code = "esim_topup_already_attempted"
    default_message = (
        "This top-up has already been sent to the eSIM platform once and cannot be sent again -- repeating it "
        "could charge the user twice. Tell the user to check their eSIMs, their data usage, their wallet "
        "balance or their order history to see what happened, and never confirm this top-up again."
    )


# ------------------------------------------------------------------------ wallet top-up
#
# Phase 6C errors, raised by the wallet top-up tools only.
#
# The money model matches the card checkout, not the wallet purchase: **opening a top-up
# page never charges anybody**, because the card is entered on the payment provider's own
# hosted page. So a failure to open one is safe to describe as a failure. What is never safe
# is to describe a top-up as received or a balance as credited -- only the platform's own
# signature-verified webhook can make either true, and these tools only ever read the result.


class WalletTopupAmountInvalidError(EsimMcpError):
    code = "wallet_topup_amount_invalid"
    default_message = (
        "The platform will not accept that top-up amount. Tell the user the amount the platform does accept, ask "
        "them for a new one, and prepare it again. Nothing was charged."
    )


class WalletTopupLimitReachedError(EsimMcpError):
    code = "wallet_topup_limit_reached"
    default_message = (
        "The platform's own top-up limit for this account has been reached, so no payment page was opened and "
        "nothing was charged. Tell the user plainly and say the limit resets after a day. Never try a smaller "
        "amount on your own initiative."
    )


class WalletTopupUnavailableError(EsimMcpError):
    """Wallet top-up is switched off, or the platform does not offer the route."""

    code = "wallet_topup_unavailable"
    default_message = (
        "Adding money to the wallet is currently unavailable at the eSIM platform, so no payment page was opened "
        "and nothing was charged. Tell the user and offer to try again shortly."
    )
    retryable = True


class WalletTopupRejectedError(EsimMcpError):
    """The platform refused to open a top-up page. Nothing was charged."""

    code = "wallet_topup_rejected"
    default_message = (
        "The eSIM platform would not open a payment page for this top-up, and nothing was charged. Tell the user "
        "it did not start and offer to try a different amount."
    )


class WalletTopupOutcomeUnknownError(EsimMcpError):
    """This server never learned whether a top-up page was created.

    Deliberately *not* an instruction to stop. Creating a page moves no money, and the
    platform recognizes a repeat of the same top-up from its own pending order row, so
    asking again returns the page it already made rather than opening a second one.
    """

    code = "wallet_topup_outcome_unknown"
    default_message = (
        "The answer to the top-up request never arrived, so this server has no payment link to hand over. "
        "Nothing has been charged and the wallet has not been credited -- a payment page is not a payment. This "
        "is a lost answer, not a refusal. Tell the user the link did not come back this time and offer to try "
        "the SAME prepared top-up again, which returns the same page rather than opening a second one."
    )
    retryable = True


class WalletTopupQuoteNotFoundError(EsimMcpError):
    """Also returned for a quote owned by somebody else -- ownership is invisible."""

    code = "wallet_topup_quote_not_found"
    default_message = (
        "No prepared top-up with that reference exists for this client. Prepare the amount again with "
        "prepare_wallet_topup and confirm it with the user before opening a payment page."
    )


class WalletTopupNotFoundError(EsimMcpError):
    """Also returned for a top-up belonging to somebody else -- ownership is invisible."""

    code = "wallet_topup_not_found"
    default_message = (
        "No wallet top-up with that reference was started by this client. Use the reference from your own "
        "create_wallet_topup_checkout result, and never take one from the user or invent one."
    )


class WalletTopupStatusUnavailableError(EsimMcpError):
    code = "wallet_topup_status_unavailable"
    default_message = (
        "The eSIM platform could not report the state of this top-up just now. Never guess whether it went "
        "through: tell the user it could not be checked and offer to check the same top-up again shortly."
    )
    retryable = True


class ForbiddenBackendRouteError(EsimMcpError):
    """A backend route this server must never call was requested. Fail closed, always.

    Raised by the transport layer itself, so it does not depend on a caller remembering the
    rule. Reaching it is a programming error, never a user action.
    """

    code = "forbidden_backend_route"
    default_message = "This server is not permitted to call that eSIM backend route."
