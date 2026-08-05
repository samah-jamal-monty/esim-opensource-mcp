"""Typed wrapper over the one wallet route this server is allowed to read.

This module exposes exactly one call: ``GET /wallet/user_wallet_by_user``, the authenticated
read of the signed-in user's own balance. It is a read, so it may use the shared bounded
retry policy.

Deliberately absent, and never to be added to this module:

* ``POST /wallet/top-up`` -- creates a Stripe PaymentIntent and mutates the wallet;
* ``GET /wallet/user_wallet_by_id/{id}`` -- unauthenticated in the backend, so it would let
  this server read *any* user's balance from an id. The authenticated per-user route is the
  only correct source, and the transport layer refuses the by-id path outright.

Any attempt to add either would be rejected by
:func:`esim_mcp.client.base.enforce_route_is_permitted` before a request left the process.
"""

from __future__ import annotations

import logging

from pydantic import SecretStr

from esim_mcp.client.base import BackendApiClient, RequestCredentials
from esim_mcp.models.wallet import UserWallet, parse_user_wallet

logger = logging.getLogger(__name__)

USER_WALLET_PATH = "/wallet/user_wallet_by_user"


class WalletApiClient:
    """The single authenticated wallet read."""

    def __init__(self, client: BackendApiClient) -> None:
        self._client = client

    async def get_user_wallet(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        locale: str,
        currency: str,
    ) -> UserWallet | None:
        """``GET /wallet/user_wallet_by_user`` in ``currency``.

        Returns ``None`` when the account has no wallet: the backend answers a successful
        envelope with ``data: null`` in that case, which is not an error.
        """
        data = await self._client.request(
            "GET",
            USER_WALLET_PATH,
            device_id=device_id,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
            allow_retry=True,
        )
        return parse_user_wallet(data)
