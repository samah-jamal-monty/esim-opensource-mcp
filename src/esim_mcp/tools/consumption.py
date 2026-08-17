"""Phase 6A MCP tool: how much data is left on an eSIM the user already owns.

This is a read, and it is the first tool in this server that can answer the question every
traveller actually asks. Before it, ``get_my_esims`` had to say plainly that the platform
reported no usage -- because the route that reports it was refused at the transport.

Where the numbers come from
---------------------------
``GET /user/consumption/{iccid}``, and nowhere else. The platform resolves the ICCID against
the caller's own profiles, finds the started (or otherwise active) bundle on that SIM, and
asks the eSIM hub for its **live** reading. Every figure in the result is copied from that
answer.

What this tool will never do
----------------------------
* **Never invent a figure.** If the platform reports nothing, the result says nothing was
  reported. There is no branch that turns silence into "0 GB used" or "full allowance left".
* **Never derive usage from a date, a price or a catalogue allowance.** A bundle's advertised
  size is what was sold, not what is left, and the gap between them is the entire question.
* **Never report a percentage it cannot compute.** The percentage comes from the platform's
  own allocated and used figures; an unlimited or unmetered plan reports none at all.
* **Never quote a stale reading.** Nothing is cached: each call is one live read.

Ownership, twice over
---------------------
The platform enforces it -- it looks the profile up as ``{user_id, iccid}`` and refuses
anything else. This tool *also* resolves the ICCID against the caller's own
``GET /user/my-esim`` list first, so a foreign or invented identifier never reaches the
platform at all. That is not redundancy for its own sake: it turns "the backend said no"
into "this is not one of your eSIMs, here are the ones that are", and it means the safety
property holds even against a future backend that forgot the check.

The ICCID is a credential-shaped identifier and is only ever returned masked (``****6789``).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from esim_mcp.client.account import AccountApiClient
from esim_mcp.client.consumption import ConsumptionApiClient
from esim_mcp.errors import (
    AuthenticationRequiredError,
    BackendTimeoutError,
    BackendUnavailableError,
    ConsumptionUnavailableError,
    EsimNotFoundError,
    InvalidInputError,
    NoConsumptionDataError,
    NoPurchasedEsimsError,
)
from esim_mcp.models.account import BackendEsim, epoch_to_iso, parse_esims
from esim_mcp.models.consumption import BackendConsumption, parse_consumption
from esim_mcp.purchase.store import QuoteOwner
from esim_mcp.selection.models import require_esim_number as _require_esim_number
from esim_mcp.selection.service import EsimSelectionService
from esim_mcp.session.identity import ClientIdentityProvider, derive_device_id
from esim_mcp.session.manager import SessionManager
from esim_mcp.settings import Settings
from esim_mcp.tools.guard import guarded
from esim_mcp.tools.purchase_preparation import user_ref_of
from esim_mcp.topup.models import mask_iccid

logger = logging.getLogger(__name__)

#: Said on every usage result. The single most important sentence in this module: these are
#: the platform's live figures, and there is no other source for them.
LIVE_READING_NOTE = (
    "These are the platform's own live figures for this eSIM, read just now. Quote them as they stand. Never "
    "adjust them, never work a figure out from a validity date or from what the plan originally included, and "
    "never describe a number here as an estimate."
)

#: What to do with a usage result.
CONSUMPTION_NEXT_STEP = (
    "Tell the user in ordinary language how much data they have used and how much is left, using the platform's "
    "own display figures. Mention the plan status and the expiry only if they are relevant to what was asked. Do "
    "not read the eSIM identifier out unless the user asks -- refer to the SIM by its plan name or its label. If "
    "they are running low and want more data, get_esim_topup_options shows what the platform sells for this SIM."
)

#: What to say when the platform genuinely has nothing to report.
NO_DATA_NEXT_STEP = (
    "Tell the user the platform has not reported any usage for this eSIM yet, which normally means the plan has "
    "not started. Do NOT say they have used nothing, do NOT say they still have their full allowance, and do NOT "
    "work a figure out from the plan or from a date -- none of those is known. Offer to check again later."
)

IccidArg = Annotated[
    str | None,
    Field(
        default=None,
        description=(
            "The identifier of the eSIM to read usage for, taken verbatim from a get_my_esims "
            "result in this conversation. Prefer esim_number instead -- this is here for the "
            "case where you already hold the identifier. Omit both when the user owns exactly "
            "one eSIM and this tool will use that one. Never invent an identifier, never take "
            "one from the user, and never pass one belonging to a different account."
        ),
    ),
]

EsimNumberArg = Annotated[
    int | None,
    Field(
        default=None,
        description=(
            "The number of the eSIM as shown in the most recent get_my_esims list for this "
            "signed-in user -- 1 for the first, 2 for the second, and so on. This is the "
            "normal way to pick one: when the user says 'number 2', pass 2. It resolves only "
            "against that user's own current list, so call get_my_esims first if nothing has "
            "been listed yet, or if the user has signed out, signed in as someone else or "
            "reconnected since. Never invent a number and never carry one over from an "
            "earlier list."
        ),
    ),
]


class ConsumptionService:
    """Backend-facing behaviour for ``get_esim_consumption``."""

    def __init__(
        self,
        settings: Settings,
        account_client: AccountApiClient,
        consumption_client: ConsumptionApiClient,
        session_manager: SessionManager,
        identity_provider: ClientIdentityProvider,
        selection_service: EsimSelectionService,
    ) -> None:
        self._settings = settings
        self._accounts = account_client
        self._consumption = consumption_client
        self._sessions = session_manager
        self._identity_provider = identity_provider
        self._selection = selection_service

    # ------------------------------------------------------------------ internals

    async def _caller(self, ctx: Any | None) -> tuple[Any, str]:
        """Resolve the verified MCP client identity and its derived device id.

        The session key comes from the transport principal and never from an argument, which
        is the whole of the multi-user isolation story here: the ICCID argument selects among
        *this caller's* eSIMs and cannot reach anybody else's.
        """
        identity = await self._identity_provider.resolve(ctx)
        return identity, derive_device_id(self._settings.salt_bytes(), identity)

    def _locale(self, value: str | None) -> str:
        return (value or "").strip() or self._settings.default_locale

    def _currency(self, value: str | None) -> str:
        return ((value or "").strip() or self._settings.default_currency).upper()

    async def _owned_esims(
        self,
        *,
        session_key: str,
        device_id: str,
        locale: str,
        currency: str,
    ) -> list[BackendEsim]:
        """Every eSIM the signed-in user owns, from the platform's own list."""

        async def operation(access_token: Any) -> Any:
            return await self._accounts.get_my_esims(
                device_id=device_id, access_token=access_token, locale=locale, currency=currency
            )

        data = await self._sessions.run_authenticated(
            session_key,
            operation,
            device_id=device_id,
            locale=locale,
            currency=currency,
            allow_refresh_replay=True,
        )
        return [esim for esim in parse_esims(data) if esim.iccid]

    @staticmethod
    def _resolve_target(esims: list[BackendEsim], iccid: str | None) -> BackendEsim:
        """Pick the eSIM to read, from the caller's own list and from nothing else.

        This is the ownership boundary. An identifier that is not in this list is reported as
        *not found* -- identical to one that does not exist anywhere -- so nothing here can
        be used to discover that another account owns a particular SIM.
        """
        if not esims:
            raise NoPurchasedEsimsError()

        requested = (iccid or "").strip()
        if not requested:
            if len(esims) == 1:
                return esims[0]
            raise InvalidInputError(
                "This account has more than one eSIM, so it is not clear which one the user means. Show the "
                "user their eSIMs from get_my_esims as a short numbered list -- plan name, label and whether "
                "each has started -- ask which one they want, and call this again with that one's identifier."
            )

        for esim in esims:
            if esim.iccid == requested:
                return esim
        logger.info("consumption_iccid_not_owned")
        raise EsimNotFoundError()

    async def _selected(self, session_key: str, esim_number: int) -> BackendEsim:
        """Turn "number 2" into one of this caller's own eSIMs, without a backend read.

        The whole point of the numbered listing: the ICCID is already known, so this path
        makes **no** ``GET /user/my-esim`` call at all. That is not only a saving -- re-reading
        would also re-number, because the platform returns eSIMs in its own order, so "number
        2" could quietly come to mean a different SIM between the listing and the follow-up.

        Ownership is not weakened by skipping the read. The listing was recorded under this
        client *and* this authenticated user, the store hands it back only to that same pair,
        and the platform still checks the ICCID against the caller's own profiles when the
        usage route is called.
        """
        session = await self._sessions.require_session(session_key)
        owner = QuoteOwner(session_key=session_key, user_ref=user_ref_of(session))
        entry = await self._selection.resolve(owner, esim_number)
        return entry.to_backend_esim()

    # ---------------------------------------------------------------------- tool

    async def get_esim_consumption(
        self,
        *,
        esim_number: int | None = None,
        iccid: str | None = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Read one owned eSIM's live usage. Reads only; changes nothing.

        Exactly one eSIM per call. ``esim_number`` selects from the caller's most recent
        numbered listing and reaches the platform in a single call; ``iccid`` is the older
        path and still resolves against a fresh listing. Passing both is refused rather than
        silently resolved one way, because the two could disagree and only the caller knows
        which one it meant.
        """
        identity, device_id = await self._caller(ctx)
        locale_value = self._locale(locale)
        currency_value = self._currency(currency)

        if esim_number is not None and (iccid or "").strip():
            raise InvalidInputError(
                "Pass either esim_number or iccid, not both -- they could name different eSIMs and this tool "
                "will not choose between them. Use the number from the most recent get_my_esims list."
            )

        if esim_number is not None:
            target = await self._selected(identity.session_key, _require_esim_number(esim_number))
        else:
            esims = await self._owned_esims(
                session_key=identity.session_key,
                device_id=device_id,
                locale=locale_value,
                currency=currency_value,
            )
            target = self._resolve_target(esims, iccid)
        assert target.iccid is not None  # guaranteed by _owned_esims / by the stored listing

        async def operation(access_token: Any) -> Any:
            return await self._consumption.get_consumption(
                device_id=device_id,
                access_token=access_token,
                iccid=target.iccid or "",
                locale=locale_value,
                currency=currency_value,
            )

        try:
            data = await self._sessions.run_authenticated(
                identity.session_key,
                operation,
                device_id=device_id,
                locale=locale_value,
                currency=currency_value,
                # Safe here in a way it is not for a purchase: repeating a read costs
                # nothing and changes nothing.
                allow_refresh_replay=True,
            )
        except AuthenticationRequiredError:
            raise
        except (BackendTimeoutError, BackendUnavailableError):
            logger.warning("consumption_unavailable")
            raise ConsumptionUnavailableError() from None
        except InvalidInputError:
            # The platform refuses an ICCID it cannot resolve to one of this user's
            # profiles. Reaching this after the ownership resolution above means the eSIM
            # exists on the account but has no provisioned profile behind it yet.
            logger.info("consumption_profile_unavailable")
            raise NoConsumptionDataError() from None

        consumption = parse_consumption(data)
        if consumption is None or not consumption.has_readings:
            logger.info("consumption_empty")
            return _no_data_result(target)

        logger.info(
            "consumption_read",
            extra={
                # Never the ICCID and never the figures: a log line is not a receipt.
                "plan_status_known": consumption.status is not None,
                "usage_percent_known": consumption.usage_percent is not None,
            },
        )
        return _consumption_result(target, consumption)


# ------------------------------------------------------------------------ result shaping


def _prune(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the backend did not answer, so an absent fact is absent rather than null."""
    return {key: value for key, value in payload.items() if value is not None and value != ""}


def _esim_reference(esim: BackendEsim) -> dict[str, Any]:
    """How the result names the SIM: masked, and by the plan the user recognizes."""
    return _prune(
        {
            "masked_iccid": mask_iccid(esim.iccid),
            "label": esim.label_name,
            "plan_name": esim.bundle_marketing_name or esim.bundle_name or esim.display_title,
            "plan_started": esim.plan_started,
            "bundle_expired": esim.bundle_expired,
            "validity_date": epoch_to_iso(esim.validity_date),
        }
    )


def _no_data_result(esim: BackendEsim) -> dict[str, Any]:
    """The honest answer when the platform reported nothing.

    Deliberately carries no usage keys at all rather than zeroed ones: a model reading
    ``used: 0`` would tell the user they have used nothing, which is a claim the platform
    never made.
    """
    return {
        "status": "no_usage_reported",
        "esim": _esim_reference(esim),
        "usage_reported": False,
        "message": "The platform has not reported any usage figures for this eSIM.",
        "next_step": NO_DATA_NEXT_STEP,
    }


def _consumption_result(esim: BackendEsim, consumption: BackendConsumption) -> dict[str, Any]:
    """The structured facts one live reading reports.

    Every value is the platform's. The display strings are preferred wherever they exist,
    because they carry the unit the platform itself chose ("5.0 GB"), and the raw numbers
    travel alongside them for a model that needs to compare rather than to read out.
    """
    usage: dict[str, Any] = _prune(
        {
            "total_data": consumption.allocated_text,
            "used_data": consumption.used_text,
            "remaining_data": consumption.remaining_text,
            "usage_percent": consumption.usage_percent,
            "plan_status": consumption.status,
            "expiry_date": consumption.expiry,
        }
    )
    amounts = _prune(
        {
            "total": str(consumption.allocated) if consumption.allocated is not None else None,
            "used": str(consumption.used) if consumption.used is not None else None,
            "remaining": str(consumption.remaining) if consumption.remaining is not None else None,
        }
    )
    if amounts:
        usage["amounts"] = amounts

    result: dict[str, Any] = {
        "status": "ok",
        "esim": _esim_reference(esim),
        "usage_reported": True,
        "usage": usage,
        "reading_note": LIVE_READING_NOTE,
        "next_step": CONSUMPTION_NEXT_STEP,
    }
    if consumption.usage_percent is None:
        result["percent_note"] = (
            "The platform did not report an allowance this reading can be expressed as a percentage of -- an "
            "unlimited plan, for instance. Give the user the figures the platform did send and do not work a "
            "percentage out yourself."
        )
    return result


def register_consumption_tools(server: MCPServer, service: ConsumptionService) -> None:
    """Bind the Phase 6A tool onto an :class:`MCPServer` instance.

    Exactly one tool, and it is a read. There is no reset, no extend, no top-up and no
    activate here -- a model cannot call what does not exist.
    """

    @server.tool(
        name="get_esim_consumption",
        title="Check how much data is left on an eSIM the user owns",
        description=(
            "Read the eSIM platform's own live usage figures for one of the signed-in user's "
            "eSIMs: how much data the plan has, how much has been used, how much is left, the "
            "plan's status and when it expires. Reads only -- it changes nothing and buys "
            "nothing.\n"
            "WHEN: the user asks about data usage or what is left -- \"how much data do I have "
            "left\", \"how much have I used\", \"is my plan still active\", \"when does my eSIM "
            "expire\".\n"
            "FIRST: the user must be signed in. Check get_login_status and run the normal login "
            "conversation if they are not.\n"
            "WHICH eSIM: omit both arguments when the user owns exactly one -- this tool finds "
            "it. When they own several, show them the numbered list from get_my_esims, ask "
            "which one, and pass that number as esim_number: 'number 2' means esim_number=2. "
            "The number resolves against that user's own most recent list, so this tool does "
            "NOT re-read their eSIMs and the number must come from a list shown in this "
            "session. Pass iccid only if you already hold the identifier, and never pass both.\n"
            "ONE eSIM PER CALL. Never call this tool for several eSIMs on your own initiative "
            "to 'check them all' -- ask the user which one they mean and read that one.\n"
            "AFTER SUCCESS: give the user the figures in ordinary language, exactly as the "
            "platform reported them. Never adjust a number, never work one out from a validity "
            "date or from what the plan originally included, and never call a figure an "
            "estimate. Identify the eSIM by its plan name, its label or its number -- do not "
            "read the identifier out unless they ask.\n"
            "IF NO USAGE IS REPORTED: say the platform has not reported any usage yet, which "
            "normally means the plan has not started. Do NOT say they have used nothing and do "
            "NOT say they still have their full allowance -- neither is known.\n"
            "IF IT CANNOT BE READ: say the figures could not be read and offer to check again. "
            "A failed reading is NOT zero usage and NOT a full allowance: never turn an error "
            "into a number, and never say the eSIM is unused because the read failed.\n"
            "IF IT ANSWERS 'esim_selection_unavailable': the numbered list this number came "
            "from is gone -- call get_my_esims to show a new list and use a number from that "
            "one. If it answers 'esim_selection_out_of_range', that number is not on the list: "
            "show the list again and ask which eSIM they mean."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def get_esim_consumption(
        esim_number: EsimNumberArg = None,
        iccid: IccidArg = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "get_esim_consumption",
            lambda: service.get_esim_consumption(
                esim_number=esim_number, iccid=iccid, locale=locale, currency=currency, ctx=ctx
            ),
        )
