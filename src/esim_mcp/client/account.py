"""The two authenticated account-history reads this server may make.

Both are existing backend routes, unchanged and unextended by this server:

* ``GET /user/my-esim`` -- every eSIM belonging to the caller, from the bearer token alone;
* ``GET /user/order-history`` -- the caller's own orders, paginated by the backend.

Both are reads, so both use the shared bounded retry policy
(:meth:`~esim_mcp.client.base.BackendApiClient.request`, ``allow_retry=True``). Neither
creates, cancels or alters anything, and neither takes an identifier from the model: the user
is resolved from the verified bearer token on the backend side, so there is no parameter here
through which one MCP user could read another's eSIMs or orders.

Deliberately absent, and not to be added to this module
------------------------------------------------------
* ``GET /user/my-esim-by-order/{order_id}`` -- refused for two independent reasons. The path
  contains ``order/``, which :data:`~esim_mcp.client.base.FORBIDDEN_PATH_MARKERS` blocks
  before any request leaves the process; and the route is declared with the backend's
  ``bearer_token_anonymous`` dependency rather than ``bearer_token``, so it is the weaker of
  the two guards. ``GET /user/my-esim`` returns the same eSIM records for the authenticated
  caller, keyed on the token, and is used instead.
* ``GET /user/consumption/{iccid}`` -- blocked by the ``consumption`` marker.
* ``POST /user/bundle-label/{code}``, ``DELETE /user/order/cancel/{id}`` and every other
  mutating user route. These tools are reads.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import SecretStr

from esim_mcp.client.base import BackendApiClient, RequestCredentials

logger = logging.getLogger(__name__)

#: Every eSIM belonging to the authenticated caller.
MY_ESIM_PATH = "/user/my-esim"

#: The authenticated caller's own orders, newest first, paginated by the backend.
ORDER_HISTORY_PATH = "/user/order-history"

#: The backend's own defaults for ``GET /user/order-history`` (``Query(1)`` / ``Query(10)``).
#: Mirrored rather than reinvented so an omitted argument behaves identically to the website.
DEFAULT_PAGE_INDEX = 1
DEFAULT_PAGE_SIZE = 10

#: Upper bound this server will ask for in one call. The backend sets no maximum, and an
#: unbounded page would put an unbounded payload into a model's context.
MAX_PAGE_SIZE = 50


class AccountApiClient:
    """The signed-in user's own eSIMs and orders. Reads only."""

    def __init__(self, client: BackendApiClient) -> None:
        self._client = client

    async def get_my_esims(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        locale: str,
        currency: str,
    ) -> Any:
        """``GET /user/my-esim`` -- the caller's eSIMs, in ``currency``.

        The envelope's ``data`` is a list, or ``null`` for an account that owns none. Both are
        successful answers and neither is an error.
        """
        return await self._client.request(
            "GET",
            MY_ESIM_PATH,
            device_id=device_id,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
            allow_retry=True,
        )

    async def get_order_history(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        locale: str,
        currency: str,
        page_index: int = DEFAULT_PAGE_INDEX,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Any:
        """``GET /user/order-history`` -- the caller's orders, using the backend's own paging.

        ``page_index`` and ``page_size`` are the backend's query parameters, passed through
        under their real names rather than re-implemented here, so paging behaves exactly as
        it does for the website and the mobile app.
        """
        return await self._client.request(
            "GET",
            ORDER_HISTORY_PATH,
            device_id=device_id,
            params={"page_index": page_index, "page_size": page_size},
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
            allow_retry=True,
        )
