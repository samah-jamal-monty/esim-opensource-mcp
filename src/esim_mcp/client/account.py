"""The two authenticated account-history reads this server may make.

Both are existing backend routes, unchanged and unextended by this server:

* ``GET /user/my-esim`` -- every eSIM belonging to the caller, from the bearer token alone;
* ``GET /user/order-history`` -- the caller's own orders, paginated by the backend.

Neither creates, cancels or alters anything, and neither takes an identifier from the model:
the user is resolved from the verified bearer token on the backend side, so there is no
parameter here through which one MCP user could read another's eSIMs or orders.

Why these two reads are timed and retried differently from every other read
---------------------------------------------------------------------------
Every other ``GET`` this server makes is a catalogue lookup the platform serves from a cache,
and :attr:`~esim_mcp.settings.Settings.read_timeout` is sized for those. These two are not
cached: the platform builds each answer per user and per request, re-reading every bundle the
account owns and re-localizing every row. On a real account that ran past the general budget,
while the portal -- which imposes none -- got the same answer.

So both reads use :attr:`~esim_mcp.settings.Settings.account_read_timeout` instead, and both
take **exactly one attempt** (``allow_retry=False``). The two go together. The shared retry
policy is three attempts, and three attempts at a two-minute budget is six minutes of a chat
client waiting to be told the platform was slow -- which is exactly the failure this module
exists to stop. One attempt in, one answer or one typed timeout out.

A timeout here is raised as :class:`~esim_mcp.errors.AccountReadTimeoutError` rather than the
generic :class:`~esim_mcp.errors.BackendTimeoutError`, because the one thing that must never
happen to it is being read as "this account owns nothing". It is a subclass, so every
existing handler still catches it.

Nothing here treats a timeout as an authentication problem. A timeout is not an
:class:`~esim_mcp.errors.AuthenticationRequiredError`, so it never reaches the session
manager's refresh-and-replay branch: the token is not rotated and the read is not repeated.
Only a real ``401`` does that, exactly once.

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
from esim_mcp.errors import AccountReadTimeoutError, BackendTimeoutError

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

    @property
    def _read_timeout(self) -> float:
        """The configured budget for these two reads. Read per call, never cached here."""
        return self._client.settings.account_read_timeout

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

        One attempt, on the account budget. A timeout raises
        :class:`~esim_mcp.errors.AccountReadTimeoutError` and nothing else happens: no second
        request, no token rotation, no empty list.
        """
        try:
            return await self._client.request(
                "GET",
                MY_ESIM_PATH,
                device_id=device_id,
                locale=locale,
                currency=currency,
                credentials=RequestCredentials(access_token=access_token),
                allow_retry=False,
                read_timeout=self._read_timeout,
            )
        except BackendTimeoutError:
            logger.warning("my_esim_read_timeout", extra={"read_timeout_seconds": self._read_timeout})
            raise AccountReadTimeoutError() from None

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

        Timed and retried exactly as :meth:`get_my_esims` is, and for the same reasons: the
        platform builds this page per user too, and "the platform was slow" must never reach
        a user as "you have never ordered anything".
        """
        try:
            return await self._client.request(
                "GET",
                ORDER_HISTORY_PATH,
                device_id=device_id,
                params={"page_index": page_index, "page_size": page_size},
                locale=locale,
                currency=currency,
                credentials=RequestCredentials(access_token=access_token),
                allow_retry=False,
                read_timeout=self._read_timeout,
            )
        except BackendTimeoutError:
            logger.warning("order_history_read_timeout", extra={"read_timeout_seconds": self._read_timeout})
            raise AccountReadTimeoutError() from None
