"""Storage contract for numbered eSIM listings, plus the in-memory implementation.

The same seam every other store in this server has: the tools talk to
:class:`~esim_mcp.selection.service.EsimSelectionService`, the service talks to this
interface, and a Redis-backed implementation could replace
:class:`InMemoryEsimSelectionStore` without a line changing above it.

Ownership is enforced *by the store*, not by its callers
--------------------------------------------------------
:meth:`EsimSelectionStore.get` takes an owner and returns a listing only when both halves of
that owner match. A listing belonging to another client, or to another eSIM account on the
same client, is not refused -- it is simply absent, and the caller is told to list the eSIMs
again. That distinction matters: "refused" would confirm that somebody else's listing exists,
and this store is one lookup away from an ICCID.

.. warning::
   :class:`InMemoryEsimSelectionStore` keeps listings in the process heap. **Every listing is
   lost when the server restarts, and listings are not shared between replicas.** That is
   safe: a lost listing costs the user one ``get_my_esims`` call and nothing else -- no money,
   no reservation, no backend state. It is also why a reconnecting client is told to list
   again rather than silently resolving a number against a listing it cannot see.
"""

from __future__ import annotations

import hmac
import logging
from abc import ABC, abstractmethod

from esim_mcp.purchase.store import QuoteOwner
from esim_mcp.selection.models import EsimSelectionList

logger = logging.getLogger(__name__)


class EsimSelectionStore(ABC):
    """Storage contract for the most recent numbered eSIM listing, per owner."""

    @abstractmethod
    async def save(self, selection: EsimSelectionList) -> None:
        """Replace the owner's listing with this one. One listing per owner, always."""

    @abstractmethod
    async def get(self, owner: QuoteOwner) -> EsimSelectionList | None:
        """The owner's listing, or ``None`` when there is none **or it is not theirs**."""

    @abstractmethod
    async def invalidate_session(self, session_key: str) -> int:
        """Drop every listing held under one MCP session key. Returns how many were dropped.

        Called on logout and on session invalidation. A listing outliving its session would
        let whoever signs in next on the same client turn a number into somebody else's
        ICCID -- which is precisely the mapping this store exists to hold.
        """

    async def aclose(self) -> None:
        """Release any backend resources. No-op by default."""
        return None


class InMemoryEsimSelectionStore(EsimSelectionStore):
    """Process-local listing store. Local/single-instance operation only.

    Keyed by session key alone, with the user reference checked on read rather than folded
    into the key. That ordering is deliberate: it means a listing left behind by a previous
    account on the same client is *found and rejected* rather than missed, so the mismatch
    can be logged and the caller told to list again, instead of the two accounts' listings
    coexisting invisibly under two keys.

    Listings are copied in and out, so a caller cannot mutate stored state by holding on to
    a returned model.
    """

    def __init__(self) -> None:
        self._selections: dict[str, EsimSelectionList] = {}

    async def save(self, selection: EsimSelectionList) -> None:
        self._selections[selection.owner_session_key] = selection.model_copy(deep=True)

    async def get(self, owner: QuoteOwner) -> EsimSelectionList | None:
        selection = self._selections.get(owner.session_key)
        if selection is None:
            return None
        if not hmac.compare_digest(selection.owner_user_ref, owner.user_ref):
            # The same MCP client, a different eSIM account. Not an error to the store: the
            # listing is simply not this owner's, and the service turns that into "list the
            # eSIMs again". Never a partial match and never a fallback.
            logger.info("esim_selection_owner_mismatch")
            return None
        return selection.model_copy(deep=True)

    async def invalidate_session(self, session_key: str) -> int:
        return 1 if self._selections.pop(session_key, None) is not None else 0

    async def aclose(self) -> None:
        self._selections.clear()
