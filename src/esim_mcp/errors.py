"""Typed application errors with MCP-safe messages.

Every error carries a stable machine-readable ``code`` and a message that is safe to
hand to an MCP client. Backend ``developerMessage`` payloads, stack traces, provider
responses, JWTs, refresh tokens and OTPs never reach these messages.
"""

from __future__ import annotations

from typing import Any

__all__ = [
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
    "CountryNotFoundError",
    "EsimMcpError",
    "ExpiredOtpError",
    "ForbiddenBackendRouteError",
    "IdempotencyConflictError",
    "IdentityUnavailableError",
    "InsufficientWalletBalanceError",
    "InternalError",
    "InvalidBackendResponseError",
    "InvalidInputError",
    "InvalidOtpError",
    "NoMatchingBundlesError",
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
    "UnsafeCheckoutLinkError",
    "UnsupportedPaymentMethodError",
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
        "The eSIM platform did not confirm whether the card payment page was opened. Nothing has been charged -- "
        "a payment page is not a payment. Do NOT prepare a new quote for the same plan; tell the user the link "
        "could not be opened and offer to try the same one again shortly."
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


class ForbiddenBackendRouteError(EsimMcpError):
    """A backend route this server must never call was requested. Fail closed, always.

    Raised by the transport layer itself, so it does not depend on a caller remembering the
    rule. Reaching it is a programming error, never a user action.
    """

    code = "forbidden_backend_route"
    default_message = "This server is not permitted to call that eSIM backend route."
