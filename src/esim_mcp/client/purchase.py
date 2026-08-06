"""The one mutating call this server may make: an idempotent, wallet-only bundle purchase.

This module wraps ``POST /api/v1/mcp/user/bundle/assign`` -- the MCP-only purchase endpoint,
which is a different route from the legacy one the mobile and web clients use. The
distinction matters and is the reason this module may exist at all:

* the legacy route has **no idempotency key and no duplicate-order protection**, so a
  retried call there can buy the same plan twice. It stays banned by
  :func:`~esim_mcp.client.base.enforce_route_is_permitted` and is never called from here;
* the MCP route **requires** an ``Idempotency-Key``, accepts wallet payment only, and replays
  a stored answer instead of executing a second purchase when the same key comes back.

Deliberately absent, and never to be added to this module: card or payment-provider flows,
top-ups, vouchers, promotions, order-OTP verification and provisioning. The request body is
built from a stored quote only -- there is no parameter through which a price, a tax amount,
a balance, a user id or a payment status could be supplied, because the platform derives
every one of them itself and rejects any unknown field outright.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import SecretStr

from esim_mcp.client.base import BackendApiClient, BackendOutcome, RequestCredentials
from esim_mcp.purchase.models import SearchContext, SearchContextKind

logger = logging.getLogger(__name__)

#: The MCP-only purchase route. Exactly this path is allowlisted in the transport guard.
MCP_PURCHASE_PATH = "/mcp/user/bundle/assign"

#: The only payment type this endpoint accepts, and the only one this phase implements.
WALLET_PAYMENT_TYPE = "Wallet"


def build_related_search(context: SearchContext) -> dict[str, Any] | None:
    """Render a stored search context into the platform's ``related_search`` shape.

    The platform accepts either a region (``iso_code`` + ``region_name``) or a list of
    countries (``iso3_code`` + ``country_name``). Both halves come from the *stored quote*,
    where they were recorded from the platform's own country and region lists at preparation
    time -- never from a display name, and never from anything the model supplied here.

    Returns ``None`` when the quote recorded no usable context: the field is optional, and
    sending a half-filled one would be worse than sending none.
    """
    if context.kind is SearchContextKind.REGION and context.region_code and context.region_name:
        return {"region": {"iso_code": context.region_code, "region_name": context.region_name}}
    if context.kind is SearchContextKind.COUNTRY and context.country_iso3 and context.country_name:
        return {"countries": [{"iso3_code": context.country_iso3, "country_name": context.country_name}]}
    return None


class PurchaseApiClient:
    """The single authenticated, idempotent wallet purchase."""

    def __init__(self, client: BackendApiClient) -> None:
        self._client = client

    async def purchase_bundle_with_wallet(
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
        read_timeout: float | None = None,
    ) -> BackendOutcome:
        """Send one purchase and return whatever the platform answered.

        **Never retried here, at any level.** One call in, one outcome out: a timeout raises,
        and the caller decides -- with the same key it passed in -- whether to ask the
        platform again. Nothing in this method may ever generate a key of its own.

        ``read_timeout`` is the caller's budget, and it is the longest one this server uses.
        The platform's shared assign flow debits the wallet and issues an eSIM against the
        eSIM hub before it replies, which takes upwards of a minute. Hanging up does not
        cancel a penny of it: the money still leaves, the eSIM is still issued, and the only
        casualty is this server's knowledge of whether either happened.

        ``currency`` is sent explicitly rather than omitted so the platform can refuse a
        purchase whose settlement currency differs from the one the quote was priced in. A
        silent currency substitution would mean charging an amount the user was never told.
        """
        body: dict[str, Any] = {
            "bundle_code": bundle_code,
            "payment_type": WALLET_PAYMENT_TYPE,
            "quote_reference": quote_reference,
        }
        related_search = build_related_search(search_context)
        if related_search is not None:
            body["related_search"] = related_search

        return await self._client.request_once(
            "POST",
            MCP_PURCHASE_PATH,
            device_id=device_id,
            json_body=body,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
            idempotency_key=idempotency_key,
            read_timeout=read_timeout,
        )
