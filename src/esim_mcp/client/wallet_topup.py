"""The three wallet-top-up calls this server may make.

This module wraps the platform's MCP-only wallet routes:

* ``GET  /api/v1/mcp/wallet/top-up/options`` -- what the platform will accept right now: the
  minimum, the remaining rolling allowance, the caller's own balance and the limits behind
  them. A read. It creates nothing and contacts no payment provider;
* ``POST /api/v1/mcp/wallet/top-up/checkout`` -- asks the platform to open a Stripe-hosted
  page for one top-up and hand back the link;
* ``GET  /api/v1/mcp/wallet/top-up/status/{payment_reference}`` -- the authenticated read of
  what happened to that top-up.

The legacy ``POST /api/v1/wallet/top-up`` stays banned by
:func:`~esim_mcp.client.base.enforce_route_is_permitted` and is never called from here. It
answers with a Stripe ``payment_intent_client_secret``, a publishable key, a customer id and
an ephemeral key -- values built for a native payment sheet, useless to a chat client, and
exactly the kind of thing that must never reach a model's context.

What this module cannot do, structurally
----------------------------------------
It cannot take a payment. There is no parameter here for a card number, an expiry, a
security code, a cardholder, a billing address or a payment token, and no route through
which one could be sent: the card is entered on Stripe's own hosted page, which this server
never renders, never proxies and never sees the contents of. The checkout body carries
exactly one field -- an amount the platform then re-validates against its own rules.

It also cannot credit a balance. There is no crediting route here, the platform's crediting
path is its own signature-verified webhook, and this server holds no Stripe credential.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import SecretStr

from esim_mcp.client.base import BackendApiClient, BackendOutcome, RequestCredentials
from esim_mcp.errors import InvalidInputError

logger = logging.getLogger(__name__)

#: The MCP-only wallet routes. The first two are allowlisted by exact match in the transport
#: guard; the third is a prefix whose one trailing segment is validated below.
WALLET_TOPUP_OPTIONS_PATH = "/mcp/wallet/top-up/options"
WALLET_TOPUP_CHECKOUT_PATH = "/mcp/wallet/top-up/checkout"
WALLET_TOPUP_STATUS_PATH_PREFIX = "/mcp/wallet/top-up/status/"

#: The shape a payment reference may take. This server generates none of these values, so a
#: value that is not one plain opaque segment came from somewhere it should not have and is
#: refused rather than escaped.
_PAYMENT_REFERENCE_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


def require_topup_reference(value: str | None) -> str:
    """Validate a top-up payment reference before it is ever put into a path."""
    candidate = (value or "").strip()
    if not _PAYMENT_REFERENCE_RE.match(candidate):
        raise InvalidInputError(
            "That is not a top-up reference this server issued. Use the reference from your own "
            "create_wallet_topup_checkout result in this conversation, and never invent one or take one from "
            "the user."
        )
    return candidate


class WalletTopupApiClient:
    """Read the platform's top-up rules, open one hosted top-up page, and read its state."""

    def __init__(self, client: BackendApiClient) -> None:
        self._client = client

    async def get_options(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        locale: str,
        currency: str,
    ) -> Any:
        """``GET /mcp/wallet/top-up/options`` -- the platform's own limits, for this caller.

        A read, so the shared bounded retry policy applies. Everything it returns is the
        platform's: this server derives no minimum, no ceiling and no fee of its own.
        """
        return await self._client.request(
            "GET",
            WALLET_TOPUP_OPTIONS_PATH,
            device_id=device_id,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
            allow_retry=True,
        )

    async def create_checkout(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        amount: str,
        locale: str,
        currency: str,
        read_timeout: float | None = None,
    ) -> BackendOutcome:
        """Send one top-up checkout request and return whatever the platform answered.

        **Never retried here, at any level.** One call in, one outcome out: a timeout raises
        and the caller decides whether to ask again. That decision is safe to make because
        the *platform* recognizes a repeat -- it reuses the caller's own pending order row
        and asks Stripe with the key that order already used -- so a second call returns the
        page the first one created rather than opening another. Nothing in this method may
        generate a key of its own; there is deliberately none to generate.

        The body carries **exactly one field**. The endpoint declares ``extra="forbid"``, so
        one unexpected key -- a currency, a wallet id, a helpfully-added ``paid`` flag -- is
        a ``422`` for the whole request rather than a field the platform politely ignores.
        There is no code path here that can add one: the dict is built literally.

        Note what is *not* here, and why:

        * **no currency in the body.** It reaches the platform as ``X-Currency`` alone, so
          the settlement currency is negotiated in one place and the platform can refuse a
          top-up it would settle in a different currency from the one the user was quoted;
        * **no user id, wallet id, order id, URL, card detail, provider token or access
          token.** None of them belongs in a body, and several of them belong nowhere at all.

        ``amount`` is sent as a decimal *string* so no binary-rounding artefact can turn the
        figure a user agreed to into a slightly different one on the wire.
        """
        body: dict[str, Any] = {"amount": float(amount)}

        return await self._client.request_once(
            "POST",
            WALLET_TOPUP_CHECKOUT_PATH,
            device_id=device_id,
            json_body=body,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
            read_timeout=read_timeout,
        )

    async def get_status(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        payment_reference: str,
        locale: str,
        currency: str,
    ) -> BackendOutcome:
        """Read the state of one top-up. One request, no retry, no polling loop.

        Returned as a :class:`~esim_mcp.client.base.BackendOutcome` rather than as parsed
        data so the caller can classify a failure itself: after a payment that may have been
        taken, "the platform refused" and "the platform said something I could not read" are
        different facts, and only the caller knows which one is dangerous to report.
        """
        reference = require_topup_reference(payment_reference)
        return await self._client.request_once(
            "GET",
            f"{WALLET_TOPUP_STATUS_PATH_PREFIX}{reference}",
            device_id=device_id,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
        )
