"""Numbered eSIM selection: "eSIM number 2" resolved safely, per client and per account.

An ICCID is a credential-shaped identifier this server never reads out. A user with several
eSIMs still has to be able to say which one they mean, so ``get_my_esims`` numbers what it
shows and this package remembers the mapping from those numbers to the full identifiers.

Every listing is scoped to one MCP client *and* one authenticated eSIM user, so a number
cannot cross clients, cannot survive a change of account, and cannot outlive the session that
produced it. When the listing is gone the answer is always the same: list the eSIMs again.
"""

from esim_mcp.selection.models import (
    EsimSelectionList,
    SelectedEsim,
    iccid_last4,
    require_esim_number,
)
from esim_mcp.selection.service import EsimSelectionService
from esim_mcp.selection.store import EsimSelectionStore, InMemoryEsimSelectionStore

__all__ = [
    "EsimSelectionList",
    "EsimSelectionService",
    "EsimSelectionStore",
    "InMemoryEsimSelectionStore",
    "SelectedEsim",
    "iccid_last4",
    "require_esim_number",
]
