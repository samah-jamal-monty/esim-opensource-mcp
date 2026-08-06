"""Two read-only account tools: the eSIMs the user owns, and the orders they placed.

Both answer questions about what the signed-in user *already has*. Neither buys, cancels,
tops up, labels, installs or activates anything, and neither has a parameter that could
identify a different user: the account is resolved from the MCP session's bearer token on the
backend side, exactly as :func:`get_user_profile` resolves it. One MCP client cannot read
another's eSIMs or orders, because there is nothing to pass that would ask for them.

Why these exist
---------------
Without them a user who has paid has no route to the thing they bought. ``create_card_checkout``
and ``confirm_purchase`` both end by reporting an order, and the platform's own
``next_action`` on a settled card payment is ``GET_ESIM_BY_ORDER`` -- an instruction that
previously named a capability this server did not have. :func:`get_my_esims` is that
capability.

What ``get_my_esims`` returns, and what it cannot
-------------------------------------------------
Everything the backend's ``EsimBundleResponse`` actually carries: the ICCID, the plan on the
SIM, whether it has started or expired, the order it came from, the dates, and the install
information (SM-DP+ address, activation code, QR value).

**No data consumption.** ``EsimBundleResponse`` carries none, and the route that does
(``GET /user/consumption/{iccid}``) is refused by the transport guard. A used/remaining figure
is therefore never reported and never inferred from a validity date.

Install information is a credential
-----------------------------------
An eSIM profile installs once. The activation code and QR value are what claim it, so the
first person to use them gets the SIM. They are returned because retrieving them is the point
of the tool, and they carry a note saying what they are. They are never written to a log line.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from esim_mcp.client.account import (
    DEFAULT_PAGE_INDEX,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AccountApiClient,
)
from esim_mcp.errors import InvalidInputError
from esim_mcp.models.account import (
    BackendEsim,
    BackendOrderHistoryEntry,
    epoch_to_iso,
    parse_esims,
    parse_order_history,
)
from esim_mcp.session.identity import ClientIdentityProvider, derive_device_id
from esim_mcp.session.manager import SessionManager
from esim_mcp.settings import Settings
from esim_mcp.tools.guard import guarded

logger = logging.getLogger(__name__)

#: Said on every eSIM result. The tool reads records; it cannot put a SIM on a phone.
INSTALL_NOTE = (
    "This assistant cannot install, activate or transfer an eSIM -- it can only read what the platform recorded. "
    "The user installs it themselves by adding an eSIM on their device and entering the SM-DP+ address and "
    "activation code below, or by scanning the QR value."
)

#: Said wherever install information appears.
CREDENTIAL_NOTE = (
    "The activation code and QR value are install credentials: an eSIM profile can be installed once, so whoever "
    "uses them first claims the SIM. Give them to the user who owns the eSIM and nobody else, and never post them "
    "anywhere shared."
)

#: What to do with an eSIM list.
ESIM_NEXT_STEP = (
    "Describe the user's eSIMs in ordinary language: the plan name, the data, the validity and whether each one "
    "has started or expired. Do not read the ICCID or the bundle code out unless the user asks for them. Give the "
    "install details only when the user wants to install a SIM, and say plainly that this assistant cannot do the "
    "installing. Never claim an eSIM is active, connected or using data: nothing here reports that."
)

#: The one thing an eSIM list cannot answer, stated so it is not guessed at.
NO_CONSUMPTION_NOTE = (
    "This does NOT include data usage. The platform does not report used or remaining data here, so never tell "
    "the user how much data they have left, and never work it out from a validity date."
)

#: What to do with an order list.
ORDER_NEXT_STEP = (
    "Summarize the user's orders in ordinary language: what plan, how much, when, and how it was paid for. Do not "
    "read order references out unless the user asks. This is a record of past orders only -- nothing here can be "
    "cancelled, refunded or repeated from this assistant, so never offer to do any of those."
)

PageIndexArg = Annotated[
    int | None,
    Field(
        default=None,
        description=(
            "Which page of orders to read, starting at 1. Omit it for the most recent page. "
            "Only pass a later page when the user asks to see older orders."
        ),
    ),
]

PageSizeArg = Annotated[
    int | None,
    Field(
        default=None,
        description=(
            "How many orders to read in one page. Omit it for the platform's own default of "
            f"{DEFAULT_PAGE_SIZE}. Keep it small: a chat reply summarizes a handful of orders, "
            f"and the user can ask for more. The maximum is {MAX_PAGE_SIZE}."
        ),
    ),
]


def _require_page_index(value: int | None) -> int:
    if value is None:
        return DEFAULT_PAGE_INDEX
    if value < 1:
        raise InvalidInputError("A page number starts at 1. Ask for page 1 to see the most recent orders.")
    return value


def _require_page_size(value: int | None) -> int:
    if value is None:
        return DEFAULT_PAGE_SIZE
    if value < 1 or value > MAX_PAGE_SIZE:
        raise InvalidInputError(
            f"A page can hold between 1 and {MAX_PAGE_SIZE} orders. Ask for a smaller page and read the rest on "
            "the next one."
        )
    return value


class AccountService:
    """Backend-facing behaviour for the two read-only account tools."""

    def __init__(
        self,
        settings: Settings,
        account_client: AccountApiClient,
        session_manager: SessionManager,
        identity_provider: ClientIdentityProvider,
    ) -> None:
        self._settings = settings
        self._accounts = account_client
        self._sessions = session_manager
        self._identity_provider = identity_provider

    # ------------------------------------------------------------------ internals

    async def _caller(self, ctx: Any | None) -> tuple[Any, str]:
        """Resolve the verified MCP client identity and its derived device id.

        The session key comes from the transport principal and never from an argument, which
        is the whole of the multi-user isolation story: two MCP clients have two session keys,
        two sessions and two bearer tokens, and neither tool accepts anything that could
        select the other one.
        """
        identity = await self._identity_provider.resolve(ctx)
        return identity, derive_device_id(self._settings.salt_bytes(), identity)

    def _locale(self, value: str | None) -> str:
        return (value or "").strip() or self._settings.default_locale

    def _currency(self, value: str | None) -> str:
        return ((value or "").strip() or self._settings.default_currency).upper()

    # ------------------------------------------------------------------- get_my_esims

    async def get_my_esims(
        self,
        *,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Read every eSIM the signed-in user owns. Reads only; installs nothing."""
        identity, device_id = await self._caller(ctx)
        locale_value = self._locale(locale)
        currency_value = self._currency(currency)

        async def operation(access_token: Any) -> Any:
            return await self._accounts.get_my_esims(
                device_id=device_id,
                access_token=access_token,
                locale=locale_value,
                currency=currency_value,
            )

        # ``allow_refresh_replay`` is safe here in a way it is not for a purchase: repeating
        # a read costs nothing and changes nothing.
        data = await self._sessions.run_authenticated(
            identity.session_key,
            operation,
            device_id=device_id,
            locale=locale_value,
            currency=currency_value,
            allow_refresh_replay=True,
        )

        esims = parse_esims(data)
        logger.info("my_esims_read", extra={"esim_count": len(esims)})

        if not esims:
            return {
                "status": "ok",
                "total_count": 0,
                "esims": [],
                "message": "This account has no eSIMs yet.",
                "next_step": (
                    "Tell the user they have no eSIMs on this account yet. If they want one, help them choose a "
                    "plan for their destination in the normal way. Never say an eSIM exists that this did not "
                    "report."
                ),
            }

        return {
            "status": "ok",
            "total_count": len(esims),
            "esims": [_esim_result(esim) for esim in esims],
            "install_note": INSTALL_NOTE,
            "credential_note": CREDENTIAL_NOTE,
            "consumption_note": NO_CONSUMPTION_NOTE,
            "next_step": ESIM_NEXT_STEP,
        }

    # --------------------------------------------------------------- get_order_history

    async def get_order_history(
        self,
        *,
        page_index: int | None = None,
        page_size: int | None = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Read a page of the signed-in user's own orders. Reads only."""
        identity, device_id = await self._caller(ctx)
        locale_value = self._locale(locale)
        currency_value = self._currency(currency)
        index = _require_page_index(page_index)
        size = _require_page_size(page_size)

        async def operation(access_token: Any) -> Any:
            return await self._accounts.get_order_history(
                device_id=device_id,
                access_token=access_token,
                locale=locale_value,
                currency=currency_value,
                page_index=index,
                page_size=size,
            )

        data = await self._sessions.run_authenticated(
            identity.session_key,
            operation,
            device_id=device_id,
            locale=locale_value,
            currency=currency_value,
            allow_refresh_replay=True,
        )

        orders = parse_order_history(data)
        logger.info("order_history_read", extra={"order_count": len(orders), "page_index": index})

        payload: dict[str, Any] = {
            "status": "ok",
            "page_index": index,
            "page_size": size,
            "returned_count": len(orders),
            "orders": [_order_result(order) for order in orders],
        }

        if not orders:
            payload["message"] = (
                "This account has no orders on this page."
                if index > DEFAULT_PAGE_INDEX
                else "This account has no orders yet."
            )
            payload["next_step"] = (
                "Tell the user there are no older orders to show."
                if index > DEFAULT_PAGE_INDEX
                else "Tell the user they have not placed any orders on this account yet. Never invent one."
            )
            return payload

        # The backend reports no total, so "there may be more" is all that can honestly be
        # said -- and only when this page came back full.
        payload["more_available"] = len(orders) == size
        payload["next_step"] = ORDER_NEXT_STEP
        return payload


# ------------------------------------------------------------------------ result shaping


def _prune(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the backend did not answer, so an absent fact is absent rather than null."""
    return {key: value for key, value in payload.items() if value is not None and value != ""}


def _esim_result(esim: BackendEsim) -> dict[str, Any]:
    """One eSIM, carrying only what the backend actually reported.

    ``plan_started`` and ``bundle_expired`` are the platform's own words and are kept
    separate: a plan that has not started is not the same as one that has expired, and
    collapsing them would tell a user their SIM is dead when it is merely unused.
    """
    plan = _prune(
        {
            "bundle_code": esim.bundle_code,
            "name": esim.bundle_marketing_name or esim.bundle_name or esim.display_title,
            "data": esim.gprs_limit_display,
            "unlimited": esim.unlimited,
            "validity": esim.validity_display or esim.validity_label,
            "plan_type": esim.plan_type,
            "activation_policy": esim.activity_policy,
            "countries_count": esim.count_countries,
            "price": esim.price_display,
            "currency": esim.currency_code,
        }
    )
    install = _prune(
        {
            "smdp_address": esim.smdp_address,
            "activation_code": esim.activation_code,
            "qr_code_value": esim.qr_code_value,
        }
    )
    result = _prune(
        {
            "iccid": esim.iccid,
            "label": esim.label_name,
            "order_reference": esim.order_number,
            "order_status": esim.order_status,
            "plan_started": esim.plan_started,
            "bundle_expired": esim.bundle_expired,
            "topup_allowed": esim.is_topup_allowed,
            "shared_with": esim.shared_with,
            "validity_date": epoch_to_iso(esim.validity_date),
            "purchased_at": epoch_to_iso(esim.payment_date),
        }
    )
    if plan:
        result["plan"] = plan
    if install:
        result["install"] = install
    if esim.transaction_history:
        result["plan_history"] = [
            _prune(
                {
                    "bundle_type": entry.bundle_type,
                    "plan_started": entry.plan_started,
                    "bundle_expired": entry.bundle_expired,
                    "added_at": epoch_to_iso(entry.created_at),
                }
            )
            for entry in esim.transaction_history
        ]
    return result


def _order_result(order: BackendOrderHistoryEntry) -> dict[str, Any]:
    """One past order, carrying only what the backend actually reported.

    The platform's own ``order_display_price`` is preferred over the raw ``order_amount``:
    it is the figure already formatted in the order's currency, which is what the user paid
    and what a receipt would show.
    """
    raw_amount = str(order.order_amount) if order.order_amount is not None else None
    result = _prune(
        {
            "order_reference": order.order_number,
            "order_status": order.order_status,
            "order_type": order.order_type,
            "amount": order.order_display_price or raw_amount,
            "currency": order.order_currency,
            "quantity": order.quantity,
            "payment_method": order.payment_type,
            "ordered_at": epoch_to_iso(order.order_date),
        }
    )
    if order.payment_details is not None:
        # Only the already-masked card description. See ``BackendPaymentDetails``.
        card = _prune(
            {
                "method": order.payment_details.payment_method,
                "brand": order.payment_details.display_brand,
                "card": order.payment_details.card_display,
            }
        )
        if card:
            result["payment_details"] = card
    if order.bundle_details is not None:
        bundle = order.bundle_details
        plan = _prune(
            {
                "bundle_code": bundle.bundle_code,
                "name": bundle.bundle_marketing_name or bundle.bundle_name or bundle.display_title,
                "data": bundle.gprs_limit_display,
                "unlimited": bundle.unlimited,
                "validity": bundle.validity_display,
                "plan_type": bundle.plan_type,
                "countries_count": bundle.count_countries,
                "price": bundle.price_display,
                "currency": bundle.currency_code,
            }
        )
        if plan:
            result["plan"] = plan
    return result


def register_account_tools(server: MCPServer, service: AccountService) -> None:
    """Bind the two read-only account tools onto an :class:`MCPServer` instance.

    Two tools, both reads. There is no install, activate, label, share, cancel, refund or
    top-up tool here -- a model cannot call what does not exist.
    """

    @server.tool(
        name="get_my_esims",
        title="List the eSIMs the signed-in user already owns",
        description=(
            "Read the eSIMs on the signed-in user's account: the plan on each one, whether it "
            "has started or expired, the order it came from, and the details needed to "
            "install it. Reads only -- it buys nothing and installs nothing.\n"
            "WHEN: the user asks about the eSIMs or plans they already have -- \"show me my "
            "eSIMs\", \"my bundles\", \"what plans do I have\", \"where is the eSIM I just "
            "bought\", \"how do I install it\", \"give me my QR code\". Also use it after a "
            "purchase is confirmed complete, when the user asks for the eSIM they paid for.\n"
            "FIRST: the user must be signed in. Check get_login_status and run the normal "
            "login conversation if they are not.\n"
            "This is about plans the user ALREADY OWNS. To show plans the platform SELLS, use "
            "find_bundles_by_country or the other catalogue tools instead.\n"
            "AFTER SUCCESS: describe the eSIMs in ordinary language -- plan name, data, "
            "validity, and whether each has started or expired. Do not read an ICCID or a "
            "bundle code out unless the user asks. Give the SM-DP+ address, activation code "
            "and QR value only when the user wants to install one, and tell them plainly that "
            "you cannot install or activate it for them.\n"
            "THIS DOES NOT REPORT DATA USAGE. The platform sends no used or remaining figure "
            "here, so never tell the user how much data is left and never work it out from a "
            "date. If the account has no eSIMs, say so plainly and never invent one."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def get_my_esims(
        locale: str | None = None,
        currency: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded("get_my_esims", lambda: service.get_my_esims(locale=locale, currency=currency, ctx=ctx))

    @server.tool(
        name="get_order_history",
        title="List the orders the signed-in user has placed",
        description=(
            "Read a page of the signed-in user's own past orders: the plan, the amount, the "
            "date, how it was paid for and what state the order is in. Reads only -- it "
            "cannot place, repeat, cancel or refund an order.\n"
            "WHEN: the user asks what they have bought or spent -- \"my orders\", \"my "
            "purchase history\", \"what did I pay\", \"show me my receipts\", \"did my order "
            "go through\".\n"
            "FIRST: the user must be signed in. Check get_login_status and run the normal "
            "login conversation if they are not.\n"
            "Omit page_index and page_size unless the user asks to see older orders; the "
            "platform's own defaults return the most recent ones.\n"
            "AFTER SUCCESS: summarize the orders in ordinary language -- what plan, how much, "
            "when, and how it was paid. Do not read order references out unless the user asks "
            "for one. If the account has no orders, say so plainly and never invent one.\n"
            "This shows ORDERS, not eSIMs. If the user wants the eSIM itself, its status or "
            "how to install it, use get_my_esims instead. Nothing here can be cancelled or "
            "refunded from this assistant, so never offer either."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def get_order_history(
        page_index: PageIndexArg = None,
        page_size: PageSizeArg = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "get_order_history",
            lambda: service.get_order_history(
                page_index=page_index, page_size=page_size, locale=locale, currency=currency, ctx=ctx
            ),
        )
