"""The guidance contract the AI caller reads: instructions, descriptions, annotations.

These tests protect the conversational behaviour of the server. They assert that the model
is told when to call each tool, what never to do, and that nothing advertises a capability
this phase does not have.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx
from mcp_types import Tool

from esim_mcp.errors import InvalidInputError
from esim_mcp.server import SERVER_INSTRUCTIONS, build_components
from esim_mcp.settings import Settings
from esim_mcp.tools.authentication import AuthenticationService
from tests.conftest import API_URL, StubIdentityProvider, auth_payload, envelope, make_jwt

AUTH_TOOL_NAMES = {
    "request_login_otp",
    "resend_login_otp",
    "verify_login_otp",
    "get_login_status",
    "get_user_profile",
    "logout",
}

CATALOG_TOOL_NAMES = {
    "list_countries",
    "list_regions",
    "browse_home_catalog",
    "find_bundles_by_country",
    "find_bundles_by_region",
    "list_cruise_bundles",
    "get_bundle_details",
}

PURCHASE_TOOL_NAMES = {
    "prepare_purchase",
    "get_prepared_purchase",
    "cancel_prepared_purchase",
}

EXECUTION_TOOL_NAMES = {"confirm_purchase"}

CARD_TOOL_NAMES = {"create_card_checkout", "check_card_payment_status"}

#: Read-only history of what the signed-in user already owns. Neither can change anything.
ACCOUNT_TOOL_NAMES = {"get_my_esims", "get_order_history"}

#: Phase 6A: the platform's live usage figures for one eSIM the caller owns. A read.
CONSUMPTION_TOOL_NAMES = {"get_esim_consumption"}

#: Phase 6B: four tools, and every one of them is free. There is deliberately no tool that
#: performs a top-up -- the platform cannot make a repeated one safe without a schema change.
ESIM_TOPUP_TOOL_NAMES = {
    "get_esim_topup_options",
    "prepare_esim_topup",
    "get_prepared_esim_topup",
    "cancel_prepared_esim_topup",
}

#: Phase 6C: quote an amount, open the platform's hosted page, read what happened.
WALLET_TOPUP_TOOL_NAMES = {
    "prepare_wallet_topup",
    "create_wallet_topup_checkout",
    "get_wallet_topup_status",
}

TOOL_NAMES = (
    AUTH_TOOL_NAMES
    | CATALOG_TOOL_NAMES
    | PURCHASE_TOOL_NAMES
    | EXECUTION_TOOL_NAMES
    | CARD_TOOL_NAMES
    | ACCOUNT_TOOL_NAMES
    | CONSUMPTION_TOOL_NAMES
    | ESIM_TOPUP_TOOL_NAMES
    | WALLET_TOPUP_TOOL_NAMES
)

#: Capabilities this server still does not have. A description may only mention one of
#: these as something the server *cannot* do, which is asserted separately.
#:
#: Each phase narrows this list rather than abandoning it. Phase 4 can complete a *wallet*
#: purchase, so "place an order" left the list -- creating one is the tool's job, and hiding
#: that from the description would be the dangerous choice. Phase 5B can open the platform's
#: hosted card checkout, so "checkout" left it too, and Phase 6 can quote a top-up and open a
#: wallet top-up page, so "top-up" left it as well. What stays out is every remaining way to
#: move money: a voucher, a promotion, a refund and provisioning.
FORBIDDEN_CAPABILITY_WORDS = (
    "provision",
    "voucher",
    "refund the",
)

#: The one capability Phase 6 quotes but cannot perform. Because "top-up" is now legitimate
#: vocabulary, the guard shifts from banning the *word* to pinning the *claim*: every tool
#: that talks about topping a SIM up must also say plainly that this assistant cannot do it.
ESIM_TOPUP_DISCLAIMERS = ("cannot", "eSIM app or on the website")


@pytest.fixture
async def tools(settings: Settings) -> list[Tool]:
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        return await components.server.list_tools()
    finally:
        await components.aclose()


def by_name(tools: list[Tool]) -> dict[str, Tool]:
    return {tool.name: tool for tool in tools}


# ------------------------------------------------------------------- server instructions


async def test_server_instructions_are_published_to_the_client(settings: Settings) -> None:
    """The SDK returns them in the `initialize` result, so the model sees them once."""
    components = build_components(settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        options = components.server._lowlevel_server.create_initialization_options()
    finally:
        await components.aclose()

    assert options.instructions == SERVER_INSTRUCTIONS
    assert options.instructions


@pytest.mark.parametrize(
    "requirement",
    [
        "eSIM assistant",
        "Never tell them to run or invoke a tool",
        "get_login_status",
        "never invent one",
        "confirm the exact amount",
        # Phase 2: catalogue behaviour.
        "Browsing needs no login",
        "never claim to be showing all of them",
        "browse_home_catalog",
        "find_bundles_by_country",
        "get_bundle_details",
        "numbered list",
        "Do not warn the user that an amount",
        # Phase 3: purchase preparation.
        "prepare_purchase",
        "Preparing is not buying",
        "Never choose a payment method for them",
        "never ask for card details",
        "no order was created and nothing was charged",
        "Never say a plan is reserved",
        # Phase 4: purchase execution.
        "confirm_purchase is the only tool that spends money",
        "explicitly agreed to that amount",
        "not agreement to be charged",
        "Wallet only",
        "does not buy the plan twice",
        "never say it succeeded and never say it failed",
        # Phase 5B: card checkout.
        "create_card_checkout",
        "check_card_payment_status",
        "Never ask the user for a card number",
        "only on Stripe's own secure page",
        "Opening the page charges nothing",
        "returns the same link, not a second page",
        "are not proof of anything",
        "Do not poll",
    ],
)
def test_instructions_cover_the_required_guidance(requirement: str) -> None:
    assert requirement in SERVER_INSTRUCTIONS


@pytest.mark.parametrize(
    "requirement",
    [
        "masked",
        "Never write out a complete email address, phone number or account id",
        "not even when the user typed it earlier",
        "Do not read one out to the user",
    ],
)
def test_instructions_forbid_repeating_a_complete_identifier(requirement: str) -> None:
    """The Phase 1 QA finding: the model repeated the full address the user had typed."""
    assert requirement in SERVER_INSTRUCTIONS


def test_instructions_forbid_asking_for_secrets() -> None:
    lowered = SERVER_INSTRUCTIONS.lower()
    assert "never ask the user for an access token" in lowered
    assert "never say an action succeeded unless a tool result confirms it" in lowered
    assert "never show them raw requests" in lowered or "never show" in lowered


#: snake_case words in the instructions that are argument names, not tool names.
#: Result *field* names the instructions may legitimately mention. Everything else that
#: looks like a tool name has to be one.
KNOWN_NON_TOOL_TERMS = {"bundle_code", "region_code"}


def test_instructions_reference_only_tools_that_exist(tools: list[Tool]) -> None:
    """Guidance must not promise a capability that is not registered."""
    referenced = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", SERVER_INSTRUCTIONS)) - KNOWN_NON_TOOL_TERMS
    assert referenced <= {tool.name for tool in tools}


#: Tools whose *choice* is not obvious from the task alone, so the overview must name them.
#: The rest (resend, profile, logout, the two list_* helpers) are reached from the Scope
#: sentence or from a sibling tool's description.
TOOLS_NAMED_IN_INSTRUCTIONS = {
    "request_login_otp",
    "verify_login_otp",
    "get_login_status",
    "browse_home_catalog",
    "find_bundles_by_country",
    "find_bundles_by_region",
    "list_cruise_bundles",
    "get_bundle_details",
    "prepare_purchase",
    "get_prepared_purchase",
    "cancel_prepared_purchase",
    "confirm_purchase",
    "create_card_checkout",
    "check_card_payment_status",
    "get_my_esims",
    "get_order_history",
    "get_esim_consumption",
    "get_esim_topup_options",
    "prepare_esim_topup",
    "prepare_wallet_topup",
    "create_wallet_topup_checkout",
    "get_wallet_topup_status",
}


def test_the_decision_critical_tools_are_named_in_the_instructions(tools: list[Tool]) -> None:
    """A tool the model is never told about is a tool it will not use at the right moment."""
    registered = {tool.name for tool in tools}
    assert registered >= TOOLS_NAMED_IN_INSTRUCTIONS
    for name in TOOLS_NAMED_IN_INSTRUCTIONS:
        assert name in SERVER_INSTRUCTIONS, f"{name} is never mentioned in the server instructions"


def test_instructions_state_the_scope() -> None:
    scope = SERVER_INSTRUCTIONS.split("Scope:", 1)[1]
    assert "browse the plan catalogue read-only" in scope
    # Anything outside the current phase may only appear as something the server cannot do.
    assert "It cannot" in scope
    for absent_capability in (
        "take card details itself",
        "complete an eSIM top-up",
        "vouchers",
        "refund",
        "activate",
        "provision",
    ):
        assert absent_capability in scope


def test_the_scope_admits_reading_usage_and_quoting_a_top_up_but_not_performing_one() -> None:
    """The Phase 6 line the model has to get right: usage and quotes are in, a top-up is not."""
    scope = SERVER_INSTRUCTIONS.split("Scope:", 1)[1]
    assert "report the live data usage" in scope
    assert "list and price the top-ups" in scope
    assert "It cannot take card details itself, complete an eSIM top-up" in scope


def test_the_scope_admits_a_wallet_top_up_page_but_never_crediting_a_balance() -> None:
    """Opening the page is in. Adding the money is the platform webhook's job, not this one's."""
    scope = SERVER_INSTRUCTIONS.split("Scope:", 1)[1]
    assert "open the platform's own secure page for adding money to their wallet" in scope
    assert "report what happened to that" in scope

    lowered = SERVER_INSTRUCTIONS.lower()
    assert "opening the page charges nothing and adds nothing to their balance" in lowered
    assert "get_wallet_topup_status is the only way to know whether a top-up went through" in lowered


def test_the_scope_admits_a_wallet_purchase_and_nothing_wider() -> None:
    """The Phase 4 line the model has to get right: a wallet buy is in, everything else is out."""
    scope = SERVER_INSTRUCTIONS.split("Scope:", 1)[1]
    assert "prepare a purchase quote" in scope
    assert "buy that prepared plan from their wallet" in scope
    # The condition is part of the capability, not a separate nicety.
    assert "explicitly agreed to the amount" in scope
    assert "confirm the exact amount with the user before anything is charged" in scope


def test_the_scope_admits_a_card_payment_but_never_taking_a_card() -> None:
    """The Phase 5B line the model has to get right: the page is in, the card is not."""
    scope = SERVER_INSTRUCTIONS.split("Scope:", 1)[1]
    assert "open the platform's own secure card payment page" in scope
    assert "report what happened to that payment" in scope
    assert "It cannot take card details itself" in scope


def test_the_scope_still_denies_every_other_way_to_move_money() -> None:
    scope = SERVER_INSTRUCTIONS.split("Scope:", 1)[1]
    for denied in (
        "take card details itself",
        "complete an eSIM top-up",
        "vouchers or promotions",
        "refund",
    ):
        assert denied in scope


@pytest.mark.parametrize("phrase", ["yes, buy it", "confirm", "pay now"])
def test_instructions_name_the_user_turns_that_authorize_a_purchase(phrase: str) -> None:
    """The exact user turns that mean "charge me", named so the model recognizes them."""
    assert phrase in SERVER_INSTRUCTIONS.lower()


def test_instructions_never_let_a_quote_request_become_a_purchase() -> None:
    """The failure this phase most has to prevent: buying because the user asked for a price."""
    lowered = SERVER_INSTRUCTIONS.lower()
    assert "is not agreement to be charged" in lowered
    assert "neither is silence" in lowered


def test_instructions_never_promise_a_reservation() -> None:
    lowered = SERVER_INSTRUCTIONS.lower()
    assert "preparing is not buying" in lowered
    assert "never say a plan is reserved, held, booked or bought" in lowered


def test_instructions_never_promise_a_complete_catalogue() -> None:
    lowered = SERVER_INSTRUCTIONS.lower()
    assert "there is no way to list every plan" in lowered
    assert "never claim to be showing all of them" in lowered


# ------------------------------------------------------------------- tool-level guidance


def test_exactly_the_authentication_and_catalogue_tools_are_exposed(tools: list[Tool]) -> None:
    assert {tool.name for tool in tools} == TOOL_NAMES


def test_every_tool_has_a_human_title_and_substantial_description(tools: list[Tool]) -> None:
    for tool in tools:
        assert tool.title, f"{tool.name} has no title"
        assert tool.description and len(tool.description) > 120, f"{tool.name} description is too thin"


@pytest.mark.parametrize(
    ("tool_name", "phrases"),
    [
        ("request_login_otp", ["WHEN:", "never invent", "do not say login is complete", "rate limited"]),
        ("resend_login_otp", ["only when the user explicitly asks", "do not call this on your own"]),
        ("verify_login_otp", ["six-digit code", "do not ask the user for an access token", "'authenticated'"]),
        ("get_login_status", ["before anything that needs a signed-in user", "do not ask the user to log in again"]),
        ("get_user_profile", ["only while", "authentication_required", "masked", "privacy:", "never restore"]),
        ("logout", ["only when the user explicitly asks", "do not log anyone out on your own"]),
        (
            "list_countries",
            ["when:", "needs no login", "do not read the whole", "ask the user where they are travelling"],
        ),
        (
            "list_regions",
            [
                "when:",
                "needs no login",
                "find_bundles_by_region",
                "region names and codes only",
                "never returns a region's plans",
                "never conclude from it that a region has none",
            ],
        ),
        (
            "browse_home_catalog",
            ["show me all bundles", "never claim to be showing all of them", "ask which country or region"],
        ),
        (
            "find_bundles_by_country",
            [
                "when:",
                "exactly as the user said it",
                "numbered list",
                "never read a code out",
                "never invent one",
                "do not warn that they may change or exclude tax",
                "needs no login",
                "never describe it as the platform's whole",
            ],
        ),
        (
            "find_bundles_by_region",
            [
                "when:",
                "only pass filters the user stated",
                "needs no login",
                "show bundles for europe",
                "bundles in asia",
                "plans for this region",
                "the only tool that returns a region's plans",
                "not that the region has none",
            ],
        ),
        ("list_cruise_bundles", ["when:", "cruise", "before promising coverage", "needs no login"]),
        (
            "get_bundle_details",
            [
                "when:",
                "never invent a code",
                "without warning that it may change or exclude tax",
                "charges anything",
                "needs no login",
            ],
        ),
        (
            "prepare_purchase",
            [
                "when:",
                "does not buy anything",
                "creates no order",
                "reserves nothing",
                "must be signed in",
                "get_login_status",
                "never pick for them",
                "never invent a code",
                "never ask the user for one",
                "nothing was charged",
                "never say the plan is reserved",
                "do not ask for card details",
                # Preparing must route to a payment tool without becoming one.
                "preparing is not paying",
                "cannot take a payment of either kind",
                "confirm_purchase for a wallet quote",
                "create_card_checkout for a card quote",
            ],
        ),
        (
            "get_prepared_purchase",
            ["when:", "never contacts the esim platform", "expired", "nothing was charged"],
        ),
        (
            "cancel_prepared_purchase",
            [
                "when:",
                "there is no order behind a prepared quote",
                "nothing is cancelled at the esim platform",
                "never tell them an order was cancelled",
                "never contacts the esim platform",
            ],
        ),
        (
            "confirm_purchase",
            [
                "when:",
                # It must say, unmistakably, that this one costs money.
                "this spends real money",
                "debits the wallet",
                "cannot be undone",
                # Consent, in the words the model will actually see from a user.
                "explicitly said yes",
                "yes, buy it",
                "never call this on your own initiative",
                "wanting a quote is not agreeing to be charged",
                # What it will not do.
                "wallet only",
                "cannot be bought",
                # Repetition safety and the ambiguous branch.
                "never buys the plan twice",
                "never say the purchase succeeded and never say it failed",
                "do not try to buy it again",
            ],
        ),
        (
            "create_card_checkout",
            [
                "when:",
                # It must be unmistakable that this one does *not* charge by itself.
                "charges nothing by itself",
                # Consent, in the words the model will actually see from a user.
                "explicitly said yes",
                "yes, pay by card",
                "never call this on your own initiative",
                "wanting a quote is not agreeing to pay",
                # The rule this phase exists to protect.
                "never ask the user for a card number",
                "security code",
                "never offer to enter one for them",
                "only on stripe's own secure hosted page",
                "this server never sees a card",
                # Repetition safety.
                "never opens a second page",
                "never asked to pay twice",
                # What to do afterwards -- including not checking straight away.
                "nothing has been charged yet",
                "do not check the payment until",
            ],
        ),
        (
            "check_card_payment_status",
            [
                "when:",
                "only way to know whether a card payment went through",
                # The four things that are not evidence, named individually.
                "never treat any of these as proof of payment",
                "browser redirect",
                "success screen",
                "user simply saying it worked",
                # No polling.
                "do not call it on a loop",
                "one check per request",
                # Every state, and the ambiguous branch.
                "never say the plan is active, installed or activated",
                "nothing was charged",
                "never say it succeeded and never say it failed",
                "contact",
            ],
        ),
        (
            "get_esim_consumption",
            [
                "when:",
                # Which SIM, and how to ask when it is unclear. The numbered list is now the
                # way a user picks one, so the guidance has to name both the list and the
                # argument that consumes a number from it.
                "omit both arguments when the user owns exactly one",
                "numbered list from get_my_esims",
                "esim_number=2",
                # One SIM per call: nothing may fan out across an account on its own.
                "one esim per call",
                "never call this tool for several esims on your own initiative",
                # The three ways a model could invent a figure, banned individually.
                "never adjust a number",
                "never work one out from a validity date",
                "never call a figure an estimate",
                # The empty answer, which is where "0 GB used" would come from.
                "do not say they have used nothing",
                "do not say they still have their full allowance",
                # And an error is never a number.
                "never turn an error into a number",
            ],
        ),
        (
            "get_esim_topup_options",
            [
                "when:",
                "reads only",
                # The rule that stops a catalogue plan being sold as a top-up.
                "this is the only source of top-up plans",
                "never offer a plan from find_bundles_by_country",
                "never present a catalogue plan as a top-up",
                "short numbered list",
            ],
        ),
        (
            "prepare_esim_topup",
            [
                "this does not top anything up",
                "creates no order",
                "adds no data to the sim",
                "never use a code from the general catalogue",
                # The ceiling, stated where the model will read it.
                "cannot complete a top-up",
                "there is no tool that does",
                "esim app or on the website",
                "never say it is reserved, queued, started or paid for",
            ],
        ),
        (
            "prepare_wallet_topup",
            [
                "when:",
                "this charges nothing",
                "does not change the user's balance",
                # The amount is the user's to state, and only theirs.
                "never choose an amount for them",
                "never round one",
                # No card, ever.
                "never ask for a card number",
                "nothing here can take a card",
                # And the confirmation gate.
                "explicitly agreed to pay it",
            ],
        ),
        (
            "create_wallet_topup_checkout",
            [
                "this charges nothing by itself",
                "credited only after they pay",
                "payment webhook",
                # The confirmation gate, and what does not count as one.
                "explicitly said",
                "wanting a quote is not agreeing to pay",
                # No card, ever.
                "never ask the user for a card number",
                "this server never sees a card",
                # The link is the deliverable.
                "safe to repeat",
                "never opens a second page",
                "result itself is the confirmation",
                "never answer that you cannot give them a link",
                # And a lost answer is not a failure.
                "that is a lost answer, not a refusal",
            ],
        ),
        (
            "get_wallet_topup_status",
            [
                "when:",
                "only way to know whether the payment went through",
                "payment webhook",
                # The four things that are not evidence, named individually.
                "never treat any of these as proof",
                "browser redirect",
                "success screen",
                "user simply saying it worked",
                # No polling.
                "do not call it on a loop",
                "one check per request",
                # Every state.
                "nothing was charged and nothing was credited",
                "never say it succeeded and never say it failed",
            ],
        ),
    ],
)
def test_tool_descriptions_carry_decision_guidance(tools: list[Tool], tool_name: str, phrases: list[str]) -> None:
    description = by_name(tools)[tool_name].description.lower()
    for phrase in phrases:
        assert phrase.lower() in description, f"{tool_name} description is missing {phrase!r}"


def test_no_preparation_tool_description_claims_a_purchase_can_be_completed(tools: list[Tool]) -> None:
    """A description may name buying only to deny it."""
    for name in PURCHASE_TOOL_NAMES:
        description = by_name(tools)[name].description.lower()
        for claim in ("purchase is complete", "order has been placed", "payment was taken", "plan is reserved"):
            assert claim not in description.replace("never say the plan is reserved", ""), f"{name}: {claim}"


def test_preparation_arguments_forbid_inventing_or_supplying_values(tools: list[Tool]) -> None:
    properties = by_name(tools)["prepare_purchase"].input_schema["properties"]

    bundle_code = properties["bundle_code"]["description"].lower()
    assert "never invent" in bundle_code
    assert "never ask the user to read one out" in bundle_code
    assert "display name" in bundle_code

    payment_method = properties["payment_method"]["description"].lower()
    assert "wallet" in payment_method
    assert "card" in payment_method
    assert "never choose on their behalf" in payment_method
    assert "never default to one" in payment_method


@pytest.mark.parametrize("tool_name", ["get_prepared_purchase", "cancel_prepared_purchase"])
def test_the_quote_reference_argument_forbids_inventing_one(tools: list[Tool], tool_name: str) -> None:
    quote_id = by_name(tools)[tool_name].input_schema["properties"]["quote_id"]["description"].lower()

    assert "returned by prepare_purchase" in quote_id
    assert "never invent" in quote_id


def test_the_confirmation_argument_ties_the_purchase_to_a_quote_the_user_agreed_to(tools: list[Tool]) -> None:
    """The one argument that can spend money must say where it comes from, and what it means."""
    reference = by_name(tools)["confirm_purchase"].input_schema["properties"]["quote_reference"]["description"].lower()

    assert "returned by prepare_purchase" in reference
    assert "never invent" in reference
    # It must not be taken from the user: a reference the user reads out is not one this
    # server prepared, priced and showed them.
    assert "never take one from the user" in reference
    assert "explicitly agreed to buy" in reference


def test_prepare_purchase_accepts_no_priced_or_secret_argument(tools: list[Tool]) -> None:
    """The model must have no way to put a price, a balance or a card into a quote."""
    arguments = set(by_name(tools)["prepare_purchase"].input_schema["properties"])

    assert arguments == {"bundle_code", "payment_method", "country", "region", "locale", "currency"}
    for forbidden in (
        "price",
        "amount",
        "displayed_amount",
        "balance",
        "wallet_balance",
        "tax",
        "discount",
        "promo_code",
        "card",
        "card_number",
        "cvv",
        "user_id",
        "device_id",
        "access_token",
        "refresh_token",
    ):
        assert forbidden not in arguments, f"prepare_purchase accepts {forbidden!r}"


def test_catalogue_arguments_tell_the_model_where_the_value_comes_from(tools: list[Tool]) -> None:
    named = by_name(tools)
    country = named["find_bundles_by_country"].input_schema["properties"]["country"]
    assert "in their own words" in country["description"].lower()
    assert "never invent" in country["description"].lower()

    region = named["find_bundles_by_region"].input_schema["properties"]["region"]
    assert "never invent a region code" in region["description"].lower()

    bundle_code = named["get_bundle_details"].input_schema["properties"]["bundle_code"]
    assert "already received" in bundle_code["description"].lower()
    assert "never invent" in bundle_code["description"].lower()


@pytest.mark.parametrize(
    "phrase",
    [
        "show bundles for europe",
        "bundles in asia",
        "plans for this region",
    ],
)
def test_the_instructions_route_a_regions_plans_to_the_region_search(phrase: str) -> None:
    """The reported failure was a routing failure: these asks have to name one tool.

    The assistant listed the regions, then answered "no bundles available" because nothing
    told it that a region's plans live behind a different tool.
    """
    instructions = SERVER_INSTRUCTIONS.lower()

    assert phrase in instructions
    sentence = next(part for part in instructions.split(".") if phrase in part)
    assert "find_bundles_by_region" in sentence


def test_the_instructions_deny_that_the_region_list_carries_plans() -> None:
    instructions = SERVER_INSTRUCTIONS.lower()

    assert "list_regions only names the regions" in instructions
    assert "never returns their plans" in instructions
    assert "never conclude from it that a region has none" in instructions


def test_the_instructions_forbid_turning_a_failed_search_into_an_empty_catalogue() -> None:
    """Requirement: a backend error is never reported to the user as "there are none"."""
    instructions = SERVER_INSTRUCTIONS.lower()

    assert 'never turn an error into "there are none available"' in instructions
    assert "only when a search tool returned successfully and said so" in instructions


def test_the_region_search_is_registered_read_only(tools: list[Tool]) -> None:
    """A catalogue tool that could be taken for a mutation would be a contract break."""
    region_search = by_name(tools)["find_bundles_by_region"]

    assert region_search.annotations is not None
    assert region_search.annotations.read_only_hint is True
    assert region_search.annotations.destructive_hint is False
    assert region_search.annotations.idempotent_hint is True


def test_bundle_limits_are_documented_where_the_model_reads_them(tools: list[Tool]) -> None:
    limit = by_name(tools)["find_bundles_by_country"].input_schema["properties"]["limit"]

    assert "default 5" in limit["description"].lower()
    assert "maximum 20" in limit["description"].lower()


OVERCLAIMS = ("every plan", "all bundles", "all plans", "the full catalogue", "the whole catalogue")
NEGATIONS = ("no ", "not ", "never", "cannot", "without")


def test_no_tool_claims_to_list_every_bundle_on_the_platform(tools: list[Tool]) -> None:
    """A description may mention a complete catalogue only to deny that one can be listed.

    Quoted text is stripped first: a description may quote what a *user* says
    ("show me all bundles") without the server claiming it can do it.
    """
    for tool in tools:
        blob = f"{tool.name} {tool.title} {tool.description}".lower()
        blob = re.sub(r"[\"'][^\"']*[\"']", " ", blob)
        for sentence in re.split(r"(?<=[.!?])\s+|\n", blob):
            for overclaim in OVERCLAIMS:
                if overclaim in sentence:
                    assert any(negation in sentence for negation in NEGATIONS), (
                        f"{tool.name} claims {overclaim!r} without denying it: {sentence!r}"
                    )


def test_catalogue_tools_state_that_browsing_needs_no_login(tools: list[Tool]) -> None:
    for name in CATALOG_TOOL_NAMES:
        assert "needs no login" in by_name(tools)[name].description.lower(), name


def test_the_card_arguments_forbid_inventing_one_or_claiming_a_payment(tools: list[Tool]) -> None:
    """Neither card argument may be invented, and neither may carry an outcome."""
    named = by_name(tools)

    quote = named["create_card_checkout"].input_schema["properties"]["quote_reference"]["description"].lower()
    assert "returned by prepare_purchase" in quote
    assert "never invent" in quote
    assert "never take one from the user" in quote
    assert "explicitly agreed to pay" in quote

    payment = named["check_card_payment_status"].input_schema["properties"]["payment_reference"]["description"].lower()
    assert "returned by create_card_checkout" in payment
    assert "never invent" in payment
    assert "never take one from the user" in payment
    # The structural half of "a redirect is not proof": there is nowhere to claim one.
    assert "no argument here for telling this tool that the payment succeeded" in payment


def test_no_card_tool_asks_for_a_card_detail_anywhere_in_its_schema(tools: list[Tool]) -> None:
    named = by_name(tools)
    for name in CARD_TOOL_NAMES:
        rendered = json.dumps(named[name].input_schema).lower()
        for forbidden in ("card_number", "cardnumber", '"cvv"', '"cvc"', "cardholder", "expiry_month", '"pan"'):
            assert forbidden not in rendered, f"{name} schema mentions {forbidden!r}"


def test_read_only_tools_are_annotated_as_such(tools: list[Tool]) -> None:
    named = by_name(tools)
    for name in ("get_login_status", "get_user_profile", "get_prepared_purchase", *CATALOG_TOOL_NAMES):
        assert named[name].annotations is not None
        assert named[name].annotations.read_only_hint is True
    for name in (
        "request_login_otp",
        "resend_login_otp",
        "verify_login_otp",
        "logout",
        "prepare_purchase",
        "cancel_prepared_purchase",
        "confirm_purchase",
        "create_card_checkout",
    ):
        assert named[name].annotations is not None
        assert named[name].annotations.read_only_hint is False
    assert named["check_card_payment_status"].annotations.read_only_hint is True
    assert named["logout"].annotations.destructive_hint is True
    # The one tool that spends money must announce itself as destructive to the client.
    assert named["confirm_purchase"].annotations.destructive_hint is True
    # Opening a payment page spends nothing, so it must not claim otherwise: a client that
    # gates destructive tools would block a page that costs the user nothing.
    assert named["create_card_checkout"].annotations.destructive_hint is False
    assert named["get_login_status"].annotations.open_world_hint is False
    # Preparation writes local state; it destroys nothing at the platform.
    assert named["prepare_purchase"].annotations.destructive_hint is False


def test_identifier_arguments_tell_the_model_where_the_value_comes_from(tools: list[Tool]) -> None:
    named = by_name(tools)
    for name in ("request_login_otp", "resend_login_otp", "verify_login_otp"):
        properties = named[name].input_schema["properties"]
        assert "never invent" in properties["email"]["description"].lower()
        assert "never invent or guess" in properties["phone"]["description"].lower()
    pin = named["verify_login_otp"].input_schema["properties"]["verification_pin"]
    assert "six-digit" in pin["description"].lower()
    assert "never guess it" in pin["description"].lower()


def test_no_tool_schema_mentions_a_token_or_client_identity(tools: list[Tool]) -> None:
    for tool in tools:
        rendered = json.dumps({"input": tool.input_schema, "output": tool.output_schema, "name": tool.name}).lower()
        for forbidden in ('"access_token"', '"refresh_token"', '"client_id"', '"device_id"', '"session_key"'):
            assert forbidden not in rendered


def test_nothing_advertises_a_capability_this_phase_lacks(tools: list[Tool]) -> None:
    for tool in tools:
        blob = f"{tool.name} {tool.title} {tool.description}".lower()
        for word in FORBIDDEN_CAPABILITY_WORDS:
            assert word not in blob, f"{tool.name} mentions {word!r}"


# --------------------------------------------------------------- structured tool results


async def test_results_are_structured_facts_not_backend_envelopes(
    service: AuthenticationService, respx_mock: respx.Router
) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))

    result = await service.request_login_otp(email="person@example.com")

    for envelope_key in ("totalCount", "developerMessage", "responseCode", "title", "data"):
        assert envelope_key not in result
    assert set(result) == {"status", "channel", "destination", "expires_in_seconds"}


async def test_requesting_a_code_never_claims_the_user_is_authenticated(
    service: AuthenticationService, respx_mock: respx.Router
) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))

    result = await service.request_login_otp(email="person@example.com")

    assert result["status"] == "otp_requested"
    assert result.get("authenticated") is not True
    assert "authenticated" not in json.dumps(result)
    assert (await service.get_login_status())["authenticated"] is False


async def test_verification_reports_authenticated(service: AuthenticationService, respx_mock: respx.Router) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=make_jwt())))
    )
    await service.request_login_otp(email="person@example.com")

    result = await service.verify_login_otp(verification_pin="123456", email="person@example.com")

    assert result["status"] == "authenticated"
    assert result["is_verified"] is True
    assert (await service.get_login_status())["authenticated"] is True


@pytest.mark.parametrize(
    ("kwargs", "expected_words"),
    [
        ({}, ("email", "phone", "ask the user")),
        ({"email": "   "}, ("email", "phone", "ask the user")),
    ],
)
async def test_missing_login_details_produce_an_actionable_error(
    service: AuthenticationService, kwargs: dict[str, str], expected_words: tuple[str, ...]
) -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        await service.request_login_otp(**kwargs)

    message = str(excinfo.value).lower()
    for word in expected_words:
        assert word in message


async def test_missing_identifier_on_verify_is_actionable(service: AuthenticationService) -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        await service.verify_login_otp(verification_pin="123456")

    message = str(excinfo.value).lower()
    assert "same email address or phone number" in message
    assert "token" not in message


async def test_a_rejected_code_does_not_trigger_an_automatic_resend(
    service: AuthenticationService, respx_mock: respx.Router
) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))
    resend_route = respx_mock.post(f"{API_URL}/auth/resend-otp").mock(
        return_value=httpx.Response(200, json=envelope(None))
    )
    verify_route = respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=httpx.Response(400, json=envelope(None, status="failed", title="OTP_INVALID", response_code=400))
    )
    await service.request_login_otp(email="person@example.com")

    with pytest.raises(Exception):  # noqa: B017 - the specific error is asserted elsewhere
        await service.verify_login_otp(verification_pin="000000", email="person@example.com")

    assert verify_route.call_count == 1
    assert resend_route.call_count == 0


async def test_a_session_is_only_ended_by_an_explicit_logout_call(
    service: AuthenticationService, respx_mock: respx.Router
) -> None:
    respx_mock.post(f"{API_URL}/auth/login").mock(return_value=httpx.Response(200, json=envelope(None)))
    respx_mock.post(f"{API_URL}/auth/verify_otp").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token=make_jwt())))
    )
    respx_mock.get(f"{API_URL}/auth/user-info").mock(
        return_value=httpx.Response(200, json=envelope(auth_payload(access_token="", refresh_token="")))
    )
    logout_route = respx_mock.post(f"{API_URL}/auth/logout").mock(return_value=httpx.Response(200, json=envelope(None)))
    await service.request_login_otp(email="person@example.com")
    await service.verify_login_otp(verification_pin="123456", email="person@example.com")

    await service.get_user_profile()
    await service.get_login_status()

    assert logout_route.call_count == 0
    assert (await service.get_login_status())["authenticated"] is True

    await service.logout()

    assert logout_route.call_count == 1
    assert (await service.get_login_status())["authenticated"] is False


# ------------------------------------------------------- QA eSIM top-up execution guidance


def test_the_default_instructions_never_name_the_execution_tool() -> None:
    """A deployment that cannot execute must not advertise the tool that would."""
    from esim_mcp.server import build_instructions

    off = build_instructions(execution_enabled=False)
    assert "confirm_esim_topup" not in off
    assert "This deployment cannot complete a top-up" in off
    assert "complete an eSIM top-up" in off.split("Scope:", 1)[1]


def test_the_qa_instructions_state_the_three_rules_that_matter() -> None:
    """Non-idempotent, call once, never retry an unknown outcome, always confirm first."""
    from esim_mcp.server import build_instructions

    on = build_instructions(execution_enabled=True).lower()

    # Non-idempotent, said in as many words.
    assert "is not idempotent" in on
    # Call it once.
    assert "at most once" in on
    assert "confirming twice would charge the user twice" in on
    # Never retry an unknown outcome.
    assert "never retry an unknown outcome" in on
    assert "do not confirm again and do not prepare a replacement" in on
    # Explicit confirmation, and what does not count as one.
    assert "wait for them to agree explicitly" in on
    assert "is not agreement" in on
    # And where to look instead of retrying.
    for tool in ("get_my_esims", "get_esim_consumption", "get_order_history"):
        assert tool in on


async def test_the_qa_instructions_still_reference_only_published_tools(qa_topup_settings) -> None:
    """The same guard the default build passes, applied to the QA build."""
    from esim_mcp.server import build_components, build_instructions

    components = build_components(qa_topup_settings, identity_provider=StubIdentityProvider("client-a"))
    try:
        published = {tool.name for tool in await components.server.list_tools()}
    finally:
        await components.aclose()

    instructions = build_instructions(execution_enabled=True)
    referenced = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", instructions)) - KNOWN_NON_TOOL_TERMS
    assert referenced <= published
    assert "confirm_esim_topup" in published
