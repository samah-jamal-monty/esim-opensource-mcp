"""Recording a numbered listing, and turning a number back into an eSIM.

Two operations and one rule between them. ``get_my_esims`` records what it showed; a
follow-up tool resolves a number against *that same owner's* recording and nothing else.
There is no fallback: a number that cannot be resolved against the caller's own current
listing is never resolved against a stale one, a partial one, or a fresh backend read.

Why there is no "just re-read the list" fallback
-------------------------------------------------
It would be easy, and it would be wrong twice over. It would make a follow-up call cost a
second ``GET /user/my-esim`` -- the slowest read this server makes -- so "usage for number 2"
would pay for a full account rebuild before asking about one SIM. And it would silently
re-number: the platform returns eSIMs in its own order, so a re-read after a purchase can
shift what "number 2" means. A number is only meaningful against the listing the user was
actually shown, so when that listing is gone the caller is told to show a new one.
"""

from __future__ import annotations

import logging

from esim_mcp.errors import EsimSelectionOutOfRangeError, EsimSelectionUnavailableError
from esim_mcp.models.account import BackendEsim, epoch_to_iso
from esim_mcp.purchase.store import QuoteOwner
from esim_mcp.selection.models import EsimSelectionList, SelectedEsim
from esim_mcp.selection.store import EsimSelectionStore

logger = logging.getLogger(__name__)


class EsimSelectionService:
    """Owns every read and write of the per-owner numbered eSIM listing."""

    def __init__(self, store: EsimSelectionStore) -> None:
        self._store = store

    # --------------------------------------------------------------------- record

    async def record(self, owner: QuoteOwner, esims: list[BackendEsim]) -> list[SelectedEsim]:
        """Number the eSIMs this listing is about to show, and remember the mapping.

        Numbering is positional and starts at 1, in exactly the order the caller will present
        them, so the number the user reads and the number stored here cannot disagree. eSIMs
        the platform sent without an identifier are skipped rather than numbered: a number
        that resolves to nothing is worse than a shorter list.

        The recording replaces any previous one for this owner outright.
        """
        entries = [
            SelectedEsim(
                number=position,
                iccid=esim.iccid or "",
                label_name=esim.label_name,
                plan_name=esim.bundle_marketing_name or esim.bundle_name or esim.display_title,
                bundle_code=esim.bundle_code,
                data_allowance=esim.gprs_limit_display,
                validity=esim.validity_display or esim.validity_label,
                order_status=esim.order_status,
                plan_started=esim.plan_started,
                bundle_expired=esim.bundle_expired,
                topup_allowed=esim.is_topup_allowed,
                validity_date=esim.validity_date,
                purchased_at=epoch_to_iso(esim.payment_date),
            )
            for position, esim in enumerate((esim for esim in esims if esim.iccid), start=1)
        ]
        await self._store.save(
            EsimSelectionList(
                owner_session_key=owner.session_key,
                owner_user_ref=owner.user_ref,
                entries=entries,
            )
        )
        logger.info("esim_selection_recorded", extra={"esim_count": len(entries)})
        return entries

    # -------------------------------------------------------------------- resolve

    async def resolve(self, owner: QuoteOwner, number: int) -> SelectedEsim:
        """Turn a number into one of this owner's own eSIMs.

        Raises :class:`~esim_mcp.errors.EsimSelectionUnavailableError` when there is no
        listing for this client-and-user pair -- nothing listed yet, signed out, reconnected,
        restarted, or signed in as somebody else -- and
        :class:`~esim_mcp.errors.EsimSelectionOutOfRangeError` when the number is not one of
        the numbers in it. The two are separate on purpose: one is fixed by listing again,
        the other by picking a different number.
        """
        selection = await self._store.get(owner)
        if selection is None:
            logger.info("esim_selection_missing")
            raise EsimSelectionUnavailableError()

        if not selection.entries:
            # A listing was recorded and it was empty. The account owns nothing to select,
            # which is a different fact from "the listing is gone".
            raise EsimSelectionOutOfRangeError(
                "The last list of this user's eSIMs was empty, so there is no eSIM to select. Tell the user they "
                "have no eSIMs on this account and never invent one."
            )

        entry = selection.entry(number)
        if entry is None:
            logger.info("esim_selection_out_of_range", extra={"esim_count": selection.count})
            raise EsimSelectionOutOfRangeError(
                f"There is no eSIM number {number} on the current list -- it shows "
                f"{selection.count} eSIM(s), numbered 1 to {selection.count}. Show the numbered list again and "
                "ask the user which one they mean. Never guess."
            )
        return entry

    # ----------------------------------------------------------------- invalidation

    async def invalidate_session(self, session_key: str) -> int:
        """Drop the listing held under one MCP session key (logout, session invalidation)."""
        dropped = await self._store.invalidate_session(session_key)
        if dropped:
            logger.info("esim_selection_invalidated_with_session")
        return dropped
