"""The two card-checkout calls this server may make: open a hosted payment page, and read it.

This module wraps the platform's MCP-only card routes:

* ``POST /api/v1/mcp/user/bundle/card/checkout`` -- asks the platform to open a hosted payment
  page for a quote and hand back the link. It requires an ``Idempotency-Key`` and replies with
  the *existing* page when the same key returns, so a repeated call cannot open a second one;
* ``GET /api/v1/mcp/user/bundle/card/status/{payment_reference}`` -- the authenticated read of
  what happened to that payment.

What this module cannot do, structurally
----------------------------------------
It cannot take a payment. There is no parameter here for a card number, an expiry, a security
code, a cardholder, a billing address or a payment token, and no route through which one could
be sent: the card is entered on the payment provider's own hosted page, which this server
never renders, never proxies and never sees the contents of. The request body is built from a
stored quote alone -- there is no amount, currency, tax, discount or user-id parameter,
because the platform derives every one of them itself.

Deliberately absent, and never to be added here: any provider-facing call, any confirm/capture
call, any refund, any webhook or callback route, and any read of a payment this client did not
itself start.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import SecretStr

from esim_mcp.client.base import BackendApiClient, BackendOutcome, RequestCredentials
from esim_mcp.client.purchase import build_related_search
from esim_mcp.errors import InvalidInputError
from esim_mcp.purchase.models import SearchContext

logger = logging.getLogger(__name__)

#: The MCP-only card checkout route. Exactly this path is allowlisted in the transport guard.
CARD_CHECKOUT_PATH = "/mcp/user/bundle/card/checkout"

#: The MCP-only card status route, without its trailing reference segment. The prefix is
#: allowlisted in the transport guard; the segment appended to it is validated below.
CARD_STATUS_PATH_PREFIX = "/mcp/user/bundle/card/status/"

#: The shape a payment reference may take. Anything else never becomes part of a URL: this
#: server generates no references itself, so a value that is not one plain opaque segment came
#: from somewhere it should not have, and is refused rather than escaped.
_PAYMENT_REFERENCE_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


def require_payment_reference(value: str | None) -> str:
    """Validate a payment reference before it is ever put into a path.

    Refused rather than quoted or escaped. A reference is only ever echoed back from a
    ``create_card_checkout`` result in the same conversation, so anything with a slash, a
    dot-segment, a query or a space in it is a bug or an injection attempt, not a payment.
    """
    candidate = (value or "").strip()
    if not _PAYMENT_REFERENCE_RE.match(candidate):
        raise InvalidInputError(
            "That is not a payment reference this server issued. Use the reference from your own "
            "create_card_checkout result in this conversation, and never invent one or take one from the user."
        )
    return candidate


class CardCheckoutApiClient:
    """Open a hosted payment page for a prepared quote, and read that payment's state."""

    def __init__(self, client: BackendApiClient) -> None:
        self._client = client

    async def create_checkout(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        idempotency_key: SecretStr,
        bundle_code: str,
        quote_reference: str,
        search_context: SearchContext,
        locale: str,
        currency: str,
    ) -> BackendOutcome:
        """Send one checkout request and return whatever the platform answered.

        **Never retried here, at any level.** One call in, one outcome out: a timeout raises,
        and the caller decides -- with the same key it passed in -- whether to ask the platform
        again. Nothing in this method may ever generate a key of its own, because a second key
        is the one way a second payment page could come into existence.

        The body carries **exactly three fields** and no more. The endpoint declares
        ``extra="forbid"``, so one unexpected key -- a helpfully-added ``payment_type``, an
        amount, a currency -- is a ``422`` for the whole request rather than a field the
        platform politely ignores. There is deliberately no code path here that can add one:
        the dict is built literally, and the only optional member is omitted rather than sent
        as ``null``.

        Note what is *not* here, and why:

        * **no payment type.** The platform fixes it internally for this route. Sending it
          would be both redundant and rejected;
        * **no amount, price, currency, tax or discount.** The platform derives every one of
          them; a figure from this side could only ever contradict it;
        * **no user id, order id, URL, card detail, provider token or access token.** None of
          them belongs in a body, and several of them belong nowhere at all.

        ``currency`` reaches the platform only as the ``X-Currency`` header, so the settlement
        currency is negotiated in one place. It is sent explicitly rather than omitted so the
        platform can refuse a checkout it would settle in a different currency from the one
        the quote was priced in -- a silent substitution would mean charging an amount the
        user was never told.
        """
        body: dict[str, Any] = {
            "bundle_code": bundle_code,
            "quote_reference": quote_reference,
        }
        related_search = build_related_search(search_context)
        if related_search is not None:
            body["related_search"] = related_search

        return await self._client.request_once(
            "POST",
            CARD_CHECKOUT_PATH,
            device_id=device_id,
            json_body=body,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
            idempotency_key=idempotency_key,
        )

    async def get_payment_status(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        payment_reference: str,
        locale: str,
        currency: str,
    ) -> BackendOutcome:
        """Read the state of one payment. One request, no retry, no polling loop.

        Returned as a :class:`~esim_mcp.client.base.BackendOutcome` rather than as parsed data
        so the caller can classify a failure itself: after a payment that may have been taken,
        "the platform refused" and "the platform said something I could not read" are
        different facts, and only the caller knows which one is dangerous to report.
        """
        reference = require_payment_reference(payment_reference)
        return await self._client.request_once(
            "GET",
            f"{CARD_STATUS_PATH_PREFIX}{reference}",
            device_id=device_id,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
        )
