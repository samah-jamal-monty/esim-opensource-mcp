"""Server wiring: the published tool surface and its argument contract."""

from __future__ import annotations

from esim_mcp.server import build_components
from esim_mcp.settings import Settings
from tests.conftest import StubIdentityProvider

AUTHENTICATION_TOOLS = {
    "request_login_otp",
    "resend_login_otp",
    "verify_login_otp",
    "get_login_status",
    "get_user_profile",
    "logout",
}

CATALOG_TOOLS = {
    "list_countries",
    "list_regions",
    "browse_home_catalog",
    "find_bundles_by_country",
    "find_bundles_by_region",
    "list_cruise_bundles",
    "get_bundle_details",
}

#: Phase 3. Exactly three, and none of them can buy anything.
PURCHASE_PREPARATION_TOOLS = {
    "prepare_purchase",
    "get_prepared_purchase",
    "cancel_prepared_purchase",
}

#: Phase 4. Exactly one tool may spend a *wallet balance*, and this is its name. A second
#: wallet-spending tool of any kind -- a top-up, a provisioning call -- is scope this server
#: must not have, and the assertions below pin that rather than leaving it to review.
PURCHASE_EXECUTION_TOOLS = {"confirm_purchase"}

#: Phase 5B. Exactly two card tools: one opens the platform's hosted payment page, one reads
#: what happened to it. Neither can take a payment itself, and there is deliberately no third
#: -- no ``pay``, no ``capture``, no ``confirm_card_payment``, no refund.
CARD_CHECKOUT_TOOLS = {"create_card_checkout", "check_card_payment_status"}

#: Read-only account history: what the user already owns, and what they already paid.
#: Exactly two, both reads. There is deliberately no install, activate, label, share,
#: cancel, refund or top-up tool alongside them -- a model cannot call what does not exist.
ACCOUNT_TOOLS = {"get_my_esims", "get_order_history"}

EXPECTED_TOOLS = (
    AUTHENTICATION_TOOLS
    | CATALOG_TOOLS
    | PURCHASE_PREPARATION_TOOLS
    | PURCHASE_EXECUTION_TOOLS
    | CARD_CHECKOUT_TOOLS
    | ACCOUNT_TOOLS
)

FORBIDDEN_ARGUMENTS = {
    "access_token",
    "refresh_token",
    "token",
    "bearer",
    "client_id",
    "client_identity",
    "device_id",
    "session_id",
    "session_key",
}


async def test_only_the_shipped_phases_worth_of_tools_are_published(settings: Settings) -> None:
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    assert {tool.name for tool in tools} == EXPECTED_TOOLS


async def test_phase_one_authentication_tools_are_all_still_published(settings: Settings) -> None:
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    assert {tool.name for tool in tools} >= AUTHENTICATION_TOOLS


async def test_phase_two_catalogue_tools_are_all_still_published(settings: Settings) -> None:
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    assert {tool.name for tool in tools} >= CATALOG_TOOLS


async def test_the_three_preparation_tools_are_all_still_published_unchanged(settings: Settings) -> None:
    """Phase 4 adds a tool; it must not remove or rename one."""
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    assert {tool.name for tool in tools} >= PURCHASE_PREPARATION_TOOLS


async def test_exactly_one_tool_can_spend_a_wallet_balance(settings: Settings) -> None:
    """Phase 4 added one tool that spends money directly. A second would be scope it must not have.

    ``create_card_checkout`` is deliberately not counted here: it opens the platform's hosted
    payment page and debits nothing. The user pays on that page, in their own browser, or they
    do not -- which is exactly why it is a different tool with different annotations.
    """
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    names = {tool.name for tool in tools}
    added = (
        names - AUTHENTICATION_TOOLS - CATALOG_TOOLS - PURCHASE_PREPARATION_TOOLS - CARD_CHECKOUT_TOOLS
        - ACCOUNT_TOOLS
    )
    assert added == PURCHASE_EXECUTION_TOOLS
    assert len(added) == 1


async def test_exactly_two_card_tools_were_added(settings: Settings) -> None:
    """Phase 5B adds a way to *start* a card payment and a way to *read* it, and nothing else."""
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    names = {tool.name for tool in tools}
    added = (
        names - AUTHENTICATION_TOOLS - CATALOG_TOOLS - PURCHASE_PREPARATION_TOOLS - PURCHASE_EXECUTION_TOOLS
        - ACCOUNT_TOOLS
    )
    assert added == CARD_CHECKOUT_TOOLS
    assert len(added) == 2


async def test_no_card_tool_accepts_a_card_detail_or_an_amount(settings: Settings) -> None:
    """The structural half of "this server never sees a card": there is nowhere to put one."""
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    named = {tool.name: tool for tool in tools}
    assert set(named["create_card_checkout"].input_schema["properties"]) == {"quote_reference"}
    assert set(named["check_card_payment_status"].input_schema["properties"]) == {"payment_reference"}

    for name in CARD_CHECKOUT_TOOLS:
        arguments = set(named[name].input_schema["properties"])
        for forbidden in (
            "card_number",
            "card",
            "pan",
            "cvv",
            "cvc",
            "expiry",
            "expiry_month",
            "expiry_year",
            "cardholder",
            "payment_token",
            "amount",
            "price",
            "currency",
            "checkout_url",
            "idempotency_key",
            "paid",
            "user_id",
            "access_token",
        ):
            assert forbidden not in arguments, f"{name} accepts {forbidden!r}"


async def test_the_card_tools_are_annotated_for_what_they_actually_do(settings: Settings) -> None:
    """Opening a page is not destructive; reading a payment is a read. Both reach the world."""
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    named = {tool.name: tool for tool in tools}
    checkout = named["create_card_checkout"]
    assert checkout.annotations.read_only_hint is False
    # It charges nothing, so it must not announce itself as destructive: a client that
    # gates destructive tools would otherwise block a page that costs the user nothing.
    assert checkout.annotations.destructive_hint is False
    # The same quote replays the same link, never a second page.
    assert checkout.annotations.idempotent_hint is True
    assert checkout.annotations.open_world_hint is True

    status = named["check_card_payment_status"]
    assert status.annotations.read_only_hint is True
    assert status.annotations.destructive_hint is False
    assert status.annotations.open_world_hint is True


async def test_every_catalogue_tool_is_annotated_read_only(settings: Settings) -> None:
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    for tool in tools:
        if tool.name in CATALOG_TOOLS:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True, f"{tool.name} is not marked read-only"
            assert tool.annotations.destructive_hint is False


async def test_no_tool_accepts_a_token_or_an_identity_argument(settings: Settings) -> None:
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    for tool in tools:
        arguments = set(tool.input_schema.get("properties", {}))
        assert not arguments & FORBIDDEN_ARGUMENTS, f"{tool.name} exposes a forbidden argument"


async def test_no_payment_provider_or_provisioning_tools_are_registered(settings: Settings) -> None:
    """Two ways to pay exist. Everything else money can do is still out.

    Each phase narrows this list rather than abandoning it. Phase 4 could no longer ban the
    vocabulary of buying -- ``confirm_purchase`` is the point of it -- and Phase 5B can no
    longer ban "card" or "checkout" for the same reason. What stays banned is every *other*
    way to move money or change an order: naming a payment provider, taking a payment here,
    top-ups, vouchers, promotions, refunds, order OTPs and anything that touches a
    provisioned eSIM. None of those is implemented, and a tool named after one would be the
    first sign that changed.
    """
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    names = {tool.name for tool in tools}
    joined = " ".join(names)
    for forbidden in (
        "pay_",
        "capture",
        "stripe",
        "voucher",
        "promo",
        "topup",
        "top_up",
        "activate",
        "provision",
        "install",
        "consumption",
        "usage",
        "callback",
        "translate",
        "refund",
        "cancel_order",
        "assign",
    ):
        assert forbidden not in joined, f"a tool name contains {forbidden!r}"

    # Every tool whose name mentions a purchase is one of the four purchase tools, and every
    # tool whose name mentions a card or a checkout is one of the two card tools.
    assert {name for name in names if "purchase" in name} == PURCHASE_PREPARATION_TOOLS | PURCHASE_EXECUTION_TOOLS
    assert {name for name in names if "card" in name or "checkout" in name} == CARD_CHECKOUT_TOOLS


async def test_exactly_one_tool_is_annotated_as_able_to_spend_money(settings: Settings) -> None:
    """``confirm_purchase`` is the only destructive, outward-facing purchase tool."""
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    named = {tool.name: tool for tool in tools}
    confirm = named["confirm_purchase"]
    assert confirm.annotations.read_only_hint is False
    # Destructive: it spends a balance and cannot be undone from here.
    assert confirm.annotations.destructive_hint is True
    # Idempotent: the same quote reference replays the first purchase, never a second one.
    assert confirm.annotations.idempotent_hint is True
    assert confirm.annotations.open_world_hint is True


async def test_confirm_purchase_takes_only_a_quote_reference(settings: Settings) -> None:
    """No price, balance, bundle, payment method or identity may be supplied at purchase time."""
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    named = {tool.name: tool for tool in tools}
    arguments = set(named["confirm_purchase"].input_schema["properties"])

    assert arguments == {"quote_reference"}
    for forbidden in (
        "price",
        "amount",
        "displayed_amount",
        "balance",
        "wallet_balance",
        "bundle_code",
        "payment_method",
        "payment_type",
        "related_search",
        "idempotency_key",
        "user_id",
        "order_id",
        "currency",
        "card",
        "access_token",
    ):
        assert forbidden not in arguments, f"confirm_purchase accepts {forbidden!r}"


async def test_preparation_tools_are_not_annotated_read_only_but_are_not_destructive(settings: Settings) -> None:
    """``prepare_purchase`` writes local state, so it is not a read; it destroys nothing."""
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        tools = await components.server.list_tools()
    finally:
        await components.aclose()

    named = {tool.name: tool for tool in tools}
    assert named["prepare_purchase"].annotations.read_only_hint is False
    assert named["prepare_purchase"].annotations.destructive_hint is False
    assert named["get_prepared_purchase"].annotations.read_only_hint is True
    # Local-only tools must not claim to reach the outside world.
    assert named["get_prepared_purchase"].annotations.open_world_hint is False
    assert named["cancel_prepared_purchase"].annotations.open_world_hint is False
