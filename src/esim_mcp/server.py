"""MCP server wiring and entry point.

Built on the official Python MCP SDK (``mcp`` 2.x), whose ergonomic server class is
:class:`mcp.server.mcpserver.MCPServer` (the successor to 1.x ``FastMCP``). Two
transports are supported: ``stdio`` for local development and Streamable HTTP for
deployments.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.mcpserver import MCPServer

from esim_mcp import __version__
from esim_mcp.client.account import AccountApiClient
from esim_mcp.client.auth import AuthApiClient
from esim_mcp.client.base import BackendApiClient
from esim_mcp.client.card import CardCheckoutApiClient
from esim_mcp.client.catalog import CatalogApiClient
from esim_mcp.client.purchase import PurchaseApiClient
from esim_mcp.client.wallet import WalletApiClient
from esim_mcp.logging_config import configure_logging
from esim_mcp.purchase.card import CardCheckoutService, CardCheckoutStore, InMemoryCardCheckoutStore
from esim_mcp.purchase.execution import (
    InMemoryPurchaseExecutionStore,
    PurchaseExecutionService,
    PurchaseExecutionStore,
)
from esim_mcp.purchase.service import PurchaseQuoteService
from esim_mcp.purchase.store import InMemoryPurchaseQuoteStore, PurchaseQuoteStore
from esim_mcp.session.identity import ClientIdentityProvider, build_identity_provider
from esim_mcp.session.manager import SessionManager
from esim_mcp.session.store import InMemorySessionStore, SessionStore
from esim_mcp.settings import Settings, Transport, get_settings
from esim_mcp.tools.account import AccountService, register_account_tools
from esim_mcp.tools.authentication import AuthenticationService, register_authentication_tools
from esim_mcp.tools.card_checkout import CardPaymentService, register_card_checkout_tools
from esim_mcp.tools.catalog import CatalogService, register_catalog_tools
from esim_mcp.tools.purchase_execution import (
    PurchaseConfirmationService,
    register_purchase_execution_tools,
)
from esim_mcp.tools.purchase_preparation import (
    PurchasePreparationService,
    register_purchase_preparation_tools,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "esim-mcp"

# Server-level instructions are supported by this SDK version and are returned in the
# `initialize` result, so they reach the model once per session. They describe how to
# behave; the per-tool descriptions describe when to call what.
SERVER_INSTRUCTIONS = """\
You are acting as an eSIM assistant: you help travellers find the right data plan, and you \
sign them in when something needs their account. These tools are your only way to act on \
the eSIM platform on the user's behalf.

How to behave:
- Talk to the user in ordinary language. Never tell them to run or invoke a tool, and never \
show them raw requests, backend responses, JSON or internal identifiers.
- Call a tool whenever an action is required or you need a fact about the user's account or \
about what the platform sells. Do not guess or make up account or plan information.
- Ask only for what the current step needs, one thing at a time.
- Never say an action succeeded unless a tool result confirms it.
- Never ask the user for an access token, a refresh token, a password or an API key, and \
never reveal secrets. Session tokens stay on this server and are never part of a tool call.

Privacy:
- Contact details come back deliberately masked, for example m***@example.com or \
+961******67. Repeat them in exactly that masked form.
- Never write out a complete email address, phone number or account id, not even when the \
user typed it earlier in this conversation. Say "your email" or use the masked value.
- Bundle codes are for your own follow-up calls. Do not read one out to the user; refer to \
a plan by its name, or by the number you gave it in your list.

Signing a user in:
- Before anything that needs a signed-in user, check get_login_status first, and do not ask \
someone to log in again when they already have a session.
- Ask the user which email address or phone number to use; never invent one. Then call \
request_login_otp, ask the user for the six-digit code they received, and call \
verify_login_otp. Login is complete only once verify_login_otp reports 'authenticated'.
- Send another code only when the user explicitly asks, and sign the user out only when the \
user explicitly asks.

Helping someone choose a plan:
- Browsing needs no login. Never ask a user to sign in just to look at plans.
- Plans are always found for a destination: a country, a region, a global plan or a cruise \
plan. There is no way to list every plan the platform sells, so never claim to be showing \
all of them. When the user asks broadly ("show me all bundles"), use browse_home_catalog to \
say what kinds of plans exist and ask where they are travelling.
- Use find_bundles_by_country for a country, find_bundles_by_region for a region and \
list_cruise_bundles for a cruise. Pass the user's own wording; the tools resolve it against \
the platform's own lists. If a destination is not recognized, offer the suggestions the tool \
returns instead of guessing.
- "Show bundles for Europe", "bundles in Asia", "plans for this region" and any other \
request for a region's plans are find_bundles_by_region, every time. list_regions only names \
the regions the platform sells for -- it never returns their plans, so never answer a \
question about a region's plans from it and never conclude from it that a region has none. \
When the user picks a region from a list you showed, pass that region's region_code, which is \
the platform's own identifier, rather than retyping the name.
- Say a destination has no plans only when a search tool returned successfully and said so. \
If a search fails, the plans could not be read: say that, and offer to try again or to look \
at another destination. Never turn an error into "there are none available".
- Present a few options as a short numbered list with data, validity and price, then ask \
whether they want details on one. When the user picks one ("the second one"), call \
get_bundle_details with that option's own bundle_code -- never invent a code, a price, a \
data allowance, a validity or a country.
- Only apply a filter the user actually asked for, and say when a result was narrowed.
- Quote the platform's prices exactly as the tools give them. Do not warn the user that an \
amount is provisional, approximate or subject to tax the platform never mentioned, and do not \
promise that it will be recalculated later. The platform makes no such statement, so neither \
should you.

Preparing a plan for purchase:
- When a signed-in user picks a real plan and wants to go ahead, use prepare_purchase. \
Preparing is not buying: it works out what the plan would cost and records the choice, and \
that is all it does.
- The user must be signed in first. Check get_login_status and run the normal login \
conversation if they are not; never ask them to sign in merely to look at plans.
- Ask whether they want to use their wallet balance or a card, and pass their answer. Never \
choose a payment method for them, and never ask for card details -- not a number, not an \
expiry, not a security code. Nothing here can take a card.
- Use the bundle_code of the plan the user actually chose, from a result already in this \
conversation. Never invent one, never work one out from a plan's name, and never ask the user \
to read one out. If it is unclear which plan they mean, ask them.
- Never supply or assume a price, a balance or a tax amount: prepare_purchase reads all of \
them from the platform itself. Do not promise that the catalogue amount is what will finally \
be charged.
- Afterwards, tell the user the plan, the amount and the payment method, and say plainly that \
no order was created and nothing was charged. Never say a plan is reserved, held, booked or \
bought. Do not prepare the same choice twice over.
- Use get_prepared_purchase to read a quote back, and cancel_prepared_purchase when the user \
no longer wants it -- that discards local information only, so never tell them an order was \
cancelled or money refunded.
- A quote is short-lived and can expire. If one has, say so and offer to prepare it again.

Buying a plan the user has agreed to:
- confirm_purchase is the only tool that spends money. It creates an order and debits the \
user's wallet balance, and nothing here can undo that or refund it.
- Never call it unless the user has heard the exact amount from a prepared quote and has \
explicitly agreed to that amount -- "yes, buy it", "confirm", "pay now". Asking to prepare, \
price or compare a plan is not agreement to be charged, and neither is silence. When in \
doubt, ask "shall I buy it for X?" and wait for the answer.
- Always read the amount and the plan back to the user and get their answer first. If the \
quote expired, prepare it again and confirm the new amount with them -- never charge an \
amount they have not heard.
- Wallet only. A quote prepared for card payment cannot be bought with confirm_purchase; use \
the card flow below for that one.
- Confirming the same prepared quote twice does not buy the plan twice: the stored result of \
the first purchase comes back. Never tell the user they were charged twice.
- Afterwards, say plainly that the plan was bought and paid for from their wallet, and give \
the plan name and the amount. Do not read the order reference out unless they ask.
- If a purchase comes back as unknown, still processing, or needing support, never say it \
succeeded and never say it failed. Do not prepare another quote for that plan and do not try \
to buy it again -- tell the user the platform is confirming it, and offer to check the same \
purchase again.

Paying by card:
- A quote prepared for card payment is paid on the eSIM platform's own secure page, not \
here. Once the user has heard the amount and explicitly agreed to pay it by card, use \
create_card_checkout and give them the link it returns.
- Show that link immediately and in full, in the same reply. The tool result is the \
confirmation: there is no separate signal that a payment page opened, loaded or was reached, \
so never wait for one, never ask the user whether a page opened, and never say you cannot \
give them a link. Whether a browser opens is up to them and is optional -- the link is the \
result.
- Never ask the user for a card number, an expiry date, a security code, a cardholder name \
or any other card detail, and never offer to type one in for them. You never see a card: \
they enter it only on Stripe's own secure page, which the link opens in their browser. If \
they try to send you card details, tell them not to and point them at the link instead.
- Opening the page charges nothing. Say so plainly, and never tell the user they have paid \
because a link exists.
- Asking for the same prepared quote again returns the same link, not a second page. Never \
tell the user to pay twice.
- check_card_payment_status is the only way to know whether a card payment went through. A \
browser redirect, a success screen, the user coming back to this conversation, or the user \
saying "I paid" are not proof of anything -- check, and say what the check reports.
- Check when the user says they have paid or asks you to check. Do not poll, and do not keep \
checking on your own.
- If the payment is received but the eSIM is not ready, say the payment went through and the \
platform is finishing the order -- never that the plan is active, installed or activated. If \
it failed, expired or was cancelled, say plainly that nothing was charged and offer to \
prepare the plan again. If it comes back unresolved or needing support, never say it \
succeeded and never say it failed: tell the user the platform is investigating it and that \
they should contact eSIM support, and do not open another payment page.

What the user already has:
- get_my_esims lists the eSIMs on their account: the plan on each, whether it has started or \
expired, and what is needed to install it. Use it whenever they ask about plans they already \
own -- "my eSIMs", "my bundles", "where is the one I just bought", "how do I install it". \
Plans the platform sells are a different question: use the catalogue tools for those.
- get_order_history lists what they have bought and paid. Use it for "my orders", "what did \
I pay", "did my order go through".
- Neither reports data usage, because the platform sends none. Never tell a user how much \
data they have left, and never work it out from a date.
- You cannot install, activate or transfer an eSIM. Give them the SM-DP+ address and \
activation code and say plainly that they add it on their own device. Treat those as \
credentials: a profile installs once, so give them only to the user who owns the eSIM.
- If an account has no eSIMs or no orders, say so plainly and never invent one.

Scope: this version can sign a user in, report login status, read their own profile and \
wallet balance, sign them out, browse the plan catalogue read-only, list the eSIMs they \
already own and the orders they have placed, prepare a purchase quote for a plan the user \
picked, buy that prepared plan from their wallet once they have explicitly agreed to the \
amount, and open the platform's own secure card payment page for a prepared card quote and \
report what happened to that payment. It cannot take card details itself, use vouchers or \
promotions, top up a wallet, refund or cancel a completed order, or activate, provision or \
check the usage of an eSIM. Say so plainly if the user asks for one of those, and never \
imply that something happened which these tools do not do. Always confirm the exact amount \
with the user before anything is charged.
"""


@dataclass(slots=True)
class ServerComponents:
    """Everything built for one server process, exposed for tests and for shutdown."""

    settings: Settings
    backend_client: BackendApiClient
    auth_client: AuthApiClient
    catalog_client: CatalogApiClient
    wallet_client: WalletApiClient
    purchase_client: PurchaseApiClient
    card_client: CardCheckoutApiClient
    account_client: AccountApiClient
    store: SessionStore
    quote_store: PurchaseQuoteStore
    execution_store: PurchaseExecutionStore
    checkout_store: CardCheckoutStore
    session_manager: SessionManager
    identity_provider: ClientIdentityProvider
    service: AuthenticationService
    catalog_service: CatalogService
    quote_service: PurchaseQuoteService
    execution_service: PurchaseExecutionService
    checkout_service: CardCheckoutService
    purchase_service: PurchasePreparationService
    confirmation_service: PurchaseConfirmationService
    card_service: CardPaymentService
    account_service: AccountService
    server: MCPServer

    async def aclose(self) -> None:
        await self.backend_client.aclose()
        await self.store.aclose()
        await self.quote_store.aclose()
        await self.execution_store.aclose()
        await self.checkout_store.aclose()


def build_components(
    settings: Settings | None = None,
    *,
    store: SessionStore | None = None,
    quote_store: PurchaseQuoteStore | None = None,
    execution_store: PurchaseExecutionStore | None = None,
    checkout_store: CardCheckoutStore | None = None,
    identity_provider: ClientIdentityProvider | None = None,
    backend_client: BackendApiClient | None = None,
) -> ServerComponents:
    """Compose the object graph. Every dependency is injectable for testing."""
    resolved_settings = settings or get_settings()
    resolved_backend = backend_client or BackendApiClient(resolved_settings)
    auth_client = AuthApiClient(resolved_backend)
    catalog_client = CatalogApiClient(resolved_backend)
    wallet_client = WalletApiClient(resolved_backend)
    purchase_client = PurchaseApiClient(resolved_backend)
    card_client = CardCheckoutApiClient(resolved_backend)
    account_client = AccountApiClient(resolved_backend)
    resolved_store = store or InMemorySessionStore()
    session_manager = SessionManager(resolved_settings, resolved_store, auth_client)
    resolved_identity = identity_provider or build_identity_provider(resolved_settings)
    service = AuthenticationService(resolved_settings, auth_client, session_manager, resolved_identity)
    # The catalogue is read-only and login-free: it needs the identity only to derive the
    # device id the backend requires, never a session or a token.
    catalog_service = CatalogService(resolved_settings, catalog_client, resolved_identity)

    # Purchase preparation is MCP-local: the quote store never leaves this process, and the
    # only backend routes reachable from here are the bundle read and the wallet read.
    resolved_quote_store = quote_store or InMemoryPurchaseQuoteStore()
    quote_service = PurchaseQuoteService(resolved_settings, resolved_quote_store)
    purchase_service = PurchasePreparationService(
        resolved_settings,
        catalog_client,
        wallet_client,
        session_manager,
        resolved_identity,
        quote_service,
    )
    # Purchase execution is the one place this server can spend money. It is deliberately
    # built from the *same* quote service the preparation tools use, so a confirmation can
    # only ever act on a quote this server itself prepared and priced.
    resolved_execution_store = execution_store or InMemoryPurchaseExecutionStore()
    execution_service = PurchaseExecutionService(resolved_execution_store)
    confirmation_service = PurchaseConfirmationService(
        resolved_settings,
        purchase_client,
        session_manager,
        resolved_identity,
        quote_service,
        execution_service,
    )

    # Card checkout is the second way this server can lead to a charge, and the only one where
    # the money moves outside this process: it opens the platform's hosted payment page and
    # then reads what happened. It shares the same quote service, so a payment page can only
    # ever be opened for a quote this server itself prepared and priced.
    resolved_checkout_store = checkout_store or InMemoryCardCheckoutStore()
    checkout_service = CardCheckoutService(resolved_checkout_store)
    card_service = CardPaymentService(
        resolved_settings,
        card_client,
        session_manager,
        resolved_identity,
        quote_service,
        checkout_service,
    )

    # Read-only account history. Deliberately built with no quote store, no execution
    # store and no checkout store: these tools answer questions about what the user
    # already owns, and there is nothing in them that could create or alter any of it.
    account_service = AccountService(
        resolved_settings,
        account_client,
        session_manager,
        resolved_identity,
    )

    # A prepared quote must not outlive the session that created it: on logout, on session
    # invalidation and on a failed token rotation, every quote of that session is cancelled,
    # and the execution and checkout records (with their idempotency keys) go with it. A key
    # surviving its session would let whoever signs in next replay somebody else's purchase,
    # or read somebody else's payment.
    session_manager.add_invalidation_listener(quote_service.invalidate_session)
    session_manager.add_invalidation_listener(execution_service.invalidate_session)
    session_manager.add_invalidation_listener(checkout_service.invalidate_session)

    components: ServerComponents | None = None

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[dict[str, object]]:
        logger.info(
            "server_starting",
            extra={
                "environment": resolved_settings.environment.value,
                "transport": resolved_settings.transport.value,
                "version": __version__,
            },
        )
        try:
            yield {"esim_mcp": components}
        finally:
            if components is not None:
                await components.aclose()
            logger.info("server_stopped")

    server = MCPServer(
        name=SERVER_NAME,
        title="eSIM MCP Server",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        log_level=resolved_settings.log_level,
        lifespan=lifespan,
    )
    register_authentication_tools(server, service)
    register_catalog_tools(server, catalog_service)
    register_purchase_preparation_tools(server, purchase_service)
    register_purchase_execution_tools(server, confirmation_service)
    register_card_checkout_tools(server, card_service)
    register_account_tools(server, account_service)

    components = ServerComponents(
        settings=resolved_settings,
        backend_client=resolved_backend,
        auth_client=auth_client,
        catalog_client=catalog_client,
        wallet_client=wallet_client,
        purchase_client=purchase_client,
        card_client=card_client,
        account_client=account_client,
        store=resolved_store,
        quote_store=resolved_quote_store,
        execution_store=resolved_execution_store,
        checkout_store=resolved_checkout_store,
        session_manager=session_manager,
        identity_provider=resolved_identity,
        service=service,
        catalog_service=catalog_service,
        quote_service=quote_service,
        execution_service=execution_service,
        checkout_service=checkout_service,
        purchase_service=purchase_service,
        confirmation_service=confirmation_service,
        card_service=card_service,
        account_service=account_service,
        server=server,
    )
    return components


def build_server(settings: Settings | None = None) -> MCPServer:
    """Convenience wrapper returning just the configured :class:`MCPServer`."""
    return build_components(settings).server


def main() -> None:
    """Console-script entry point."""
    settings = get_settings()
    configure_logging(settings.log_level)

    if settings.transport is Transport.STREAMABLE_HTTP:
        # Imported here: esim_mcp.http_app imports this module to build the components.
        from esim_mcp.http_app import serve

        # The deployed path goes through the same ASGI app a platform start command runs,
        # so `python -m esim_mcp.server` and `uvicorn esim_mcp.http_app:create_app` serve
        # exactly the same routes -- /mcp and /health -- on the same host and port.
        serve(settings)
    else:
        build_components(settings).server.run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
