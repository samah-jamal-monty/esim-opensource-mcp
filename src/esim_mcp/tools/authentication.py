"""Phase 1 MCP tools: multi-user OTP authentication.

Contract enforced by this layer:

* no tool accepts an access token, a refresh token or a client id as an argument;
* no tool returns a token;
* the session is always selected by the *verified* MCP client identity, so one caller
  can never read or overwrite another caller's session;
* the OTP is never stored and never logged.

:class:`AuthenticationService` holds the behaviour and is directly unit-testable;
:func:`register_authentication_tools` is the thin MCP binding.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from esim_mcp.client.auth import AuthApiClient
from esim_mcp.errors import InvalidInputError
from esim_mcp.models.auth import build_login_request, build_verify_request
from esim_mcp.safety.redaction import mask_email, mask_identifier, mask_phone
from esim_mcp.session.identity import ClientIdentity, ClientIdentityProvider, derive_device_id
from esim_mcp.session.manager import SessionManager, session_key_ref
from esim_mcp.session.models import LoginChallenge
from esim_mcp.settings import Settings
from esim_mcp.tools.guard import guarded

logger = logging.getLogger(__name__)

# Argument descriptions are part of the tool contract the model reads. They say where the
# value must come from ("what the user gave you") rather than merely naming its type.
EmailArg = Annotated[
    str | None,
    Field(
        description=(
            "The user's email address, exactly as they gave it. Ask the user for it if you do "
            "not have it yet; never invent, guess or auto-complete an address."
        )
    ),
]
PhoneArg = Annotated[
    str | None,
    Field(
        description=(
            "The user's phone number in international format, for example +CCXXXXXXXXX, exactly "
            "as they gave it. Never invent or guess a number."
        )
    ),
]
ChannelArg = Annotated[
    str | None,
    Field(
        description=(
            "Delivery channel for the code: 'EMAIL' or 'SMS'. Omit it unless the user asked for a "
            "specific channel -- email logins default to EMAIL and phone logins to SMS."
        )
    ),
]
PinArg = Annotated[
    str,
    Field(
        description=(
            "The six-digit code the user read out from their email or SMS. Digits only. Never "
            "guess it, and never ask the user for any other credential or token."
        )
    ),
]
LocaleArg = Annotated[
    str | None,
    Field(description="Optional language tag for platform messages, e.g. 'en'. Omit to use the server default."),
]
CurrencyArg = Annotated[
    str | None,
    Field(description="Optional ISO-4217 currency code, e.g. 'USD'. Omit to use the server default."),
]


class AuthenticationService:
    """Backend-facing behaviour for the six Phase 1 authentication tools."""

    def __init__(
        self,
        settings: Settings,
        auth_client: AuthApiClient,
        session_manager: SessionManager,
        identity_provider: ClientIdentityProvider,
    ) -> None:
        self._settings = settings
        self._auth_client = auth_client
        self._sessions = session_manager
        self._identity_provider = identity_provider

    # ------------------------------------------------------------------ internals

    async def _caller(self, ctx: Any | None) -> tuple[ClientIdentity, str]:
        """Resolve the verified identity and its stable device id.

        Fails closed: in production an unauthenticated transport raises rather than
        falling back to a shared or guessable identity.
        """
        identity = await self._identity_provider.resolve(ctx)
        device_id = derive_device_id(self._settings.salt_bytes(), identity)
        return identity, device_id

    def _locale(self, locale: str | None) -> str:
        return (locale or self._settings.default_locale).strip()

    def _currency(self, currency: str | None) -> str:
        return (currency or self._settings.default_currency).strip().upper()

    # ---------------------------------------------------------------------- tools

    async def request_login_otp(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        otp_channel: str | None = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Ask the backend to send a one-time code. Never auto-retried (rate limited)."""
        login_request = build_login_request(email, phone, otp_channel)
        identity, device_id = await self._caller(ctx)
        locale_value = self._locale(locale)
        self._currency(currency)  # validated for symmetry with the other tools

        await self._auth_client.login(login_request, device_id=device_id, locale=locale_value)

        masked = mask_identifier(login_request.identifier) or ""
        challenge = LoginChallenge(
            masked_identifier=masked,
            login_type=login_request.login_type,
            otp_channel=login_request.resolved_channel(),
            device_id=device_id,
            ttl_seconds=self._settings.login_challenge_ttl_seconds,
        )
        await self._sessions.store_challenge(identity.session_key, challenge)
        logger.info(
            "login_otp_requested",
            extra={"session_ref": session_key_ref(identity.session_key), "channel": challenge.otp_channel.value},
        )
        return {
            "status": "otp_requested",
            "channel": challenge.otp_channel.value,
            "destination": masked,
            "expires_in_seconds": challenge.ttl_seconds,
        }

    async def resend_login_otp(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        otp_channel: str | None = None,
        locale: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Resend the code for an existing, unexpired login challenge."""
        identity, device_id = await self._caller(ctx)
        challenge = await self._sessions.get_challenge(identity.session_key)
        if challenge is None:
            raise InvalidInputError("There is no pending login request for this client. Call request_login_otp first.")

        login_request = build_login_request(email, phone, otp_channel or challenge.otp_channel.value)
        masked = mask_identifier(login_request.identifier) or ""
        if masked != challenge.masked_identifier:
            raise InvalidInputError("The supplied identifier does not match the pending login request for this client.")

        await self._auth_client.resend_otp(login_request, device_id=device_id, locale=self._locale(locale))

        renewed = LoginChallenge(
            masked_identifier=masked,
            login_type=login_request.login_type,
            otp_channel=login_request.resolved_channel(),
            device_id=device_id,
            ttl_seconds=self._settings.login_challenge_ttl_seconds,
        )
        await self._sessions.store_challenge(identity.session_key, renewed)
        logger.info(
            "login_otp_resent",
            extra={"session_ref": session_key_ref(identity.session_key), "channel": renewed.otp_channel.value},
        )
        return {
            "status": "otp_resent",
            "channel": renewed.otp_channel.value,
            "destination": masked,
            "expires_in_seconds": renewed.ttl_seconds,
        }

    async def verify_login_otp(
        self,
        *,
        verification_pin: str,
        email: str | None = None,
        phone: str | None = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Exchange the code for a server-side session. Tokens never leave the server."""
        verify_request = build_verify_request(email, phone, verification_pin)
        identity, device_id = await self._caller(ctx)

        challenge = await self._sessions.get_challenge(identity.session_key)
        if challenge is not None:
            masked = mask_identifier(verify_request.identifier) or ""
            if masked != challenge.masked_identifier:
                raise InvalidInputError(
                    "The supplied identifier does not match the pending login request for this client."
                )

        currency_value = self._currency(currency)
        auth = await self._auth_client.verify_otp(
            verify_request,
            device_id=device_id,
            locale=self._locale(locale),
            currency=currency_value,
        )
        # The request object (and with it the PIN) goes out of scope here; it is never
        # written to the store.
        session = await self._sessions.create_session(
            identity,
            device_id=device_id,
            auth=auth,
            currency=currency_value,
            fallback_email=verify_request.user_email,
            fallback_phone=verify_request.phone,
        )
        return {
            "status": "authenticated",
            "is_verified": session.is_verified,
            "user": session.safe_profile(),
        }

    async def get_login_status(self, *, ctx: Any | None = None) -> dict[str, Any]:
        """Report this client's session state without calling the backend."""
        identity, _ = await self._caller(ctx)
        session = await self._sessions.get_session(identity.session_key)
        if session is None:
            challenge = await self._sessions.get_challenge(identity.session_key)
            return {
                "authenticated": False,
                "status": "otp_pending" if challenge else "unauthenticated",
                "pending_login": (
                    {
                        "destination": challenge.masked_identifier,
                        "channel": challenge.otp_channel.value,
                        "expires_in_seconds": challenge.seconds_remaining(),
                    }
                    if challenge
                    else None
                ),
            }
        return {
            "authenticated": True,
            "status": "authenticated",
            "user": session.safe_profile(),
            "session_expires_at": session.expires_at.isoformat() if session.expires_at else None,
            "seconds_until_expiry": session.seconds_until_expiry(),
            "refresh_required": session.needs_refresh(self._settings.token_refresh_window_seconds),
            "session_started_at": session.created_at.isoformat(),
        }

    async def get_user_profile(
        self,
        *,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Fetch the profile from ``GET /auth/user-info``, refreshing the token if needed."""
        identity, device_id = await self._caller(ctx)
        locale_value = self._locale(locale)
        currency_value = self._currency(currency)

        async def operation(access_token: Any) -> Any:
            return await self._auth_client.get_user_info(
                device_id=device_id,
                access_token=access_token,
                locale=locale_value,
                currency=currency_value,
            )

        auth = await self._sessions.run_authenticated(
            identity.session_key,
            operation,
            device_id=device_id,
            locale=locale_value,
            currency=currency_value,
            allow_refresh_replay=True,
        )
        session = await self._sessions.get_session(identity.session_key)
        info = auth.user_info
        # Masked at the point of use, not merely inherited from the session: the backend
        # payload holds the full address, and no branch of this function may return it.
        profile: dict[str, Any] = {
            "user_id": (session.safe_profile()["user_id"] if session else None),
            "email": mask_email(info.email) or (session.masked_email if session else None),
            "phone": mask_phone(info.msisdn) or (session.masked_phone if session else None),
            "first_name": info.first_name,
            "last_name": info.last_name,
            "country": info.country,
            "language": info.language,
            "is_verified": auth.is_verified or info.is_verified,
        }
        wallet = (
            {"balance": info.balance, "currency": info.currency_code or currency_value}
            if info.balance is not None
            else None
        )
        return {"status": "ok", "user": profile, "wallet": wallet}

    async def logout(self, *, locale: str | None = None, ctx: Any | None = None) -> dict[str, Any]:
        """Log out the calling client only, dropping the local session regardless."""
        identity, device_id = await self._caller(ctx)
        acknowledged = await self._sessions.logout(
            identity.session_key, device_id=device_id, locale=self._locale(locale)
        )
        return {"status": "logged_out", "backend_confirmed": acknowledged}


def register_authentication_tools(server: MCPServer, service: AuthenticationService) -> None:
    """Bind the Phase 1 tools onto an :class:`MCPServer` instance.

    Descriptions, argument descriptions and tool annotations are written for an *AI
    caller*: they say when to reach for the tool, what to ask the user first, what to tell
    the user afterwards, and what never to do. The tools themselves return short structured
    facts -- the model does the phrasing.
    """

    _guard = guarded

    @server.tool(
        name="request_login_otp",
        title="Send eSIM login code",
        description=(
            "Start login: ask the eSIM platform to send a six-digit one-time code to the user's "
            "email address or phone number.\n"
            "WHEN: the user wants to log in or sign in, or something you are about to do needs a "
            "signed-in user and get_login_status reports nobody is signed in.\n"
            "FIRST: ask the user which email address or phone number to use if they have not said "
            "yet. Pass exactly one of them, exactly as the user gave it. Never invent, guess or "
            "auto-complete an address or number.\n"
            "AFTER SUCCESS: tell the user that a code was sent to the masked destination in the "
            "result and ask them to read out the six-digit code, then call verify_login_otp. The "
            "user is NOT logged in yet -- do not say login is complete.\n"
            "DO NOT call this tool again for the same login attempt; it is rate limited. If the "
            "user says the code never arrived, use resend_login_otp."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        ),
    )
    async def request_login_otp(
        email: EmailArg = None,
        phone: PhoneArg = None,
        otp_channel: ChannelArg = None,
        locale: LocaleArg = None,
        currency: CurrencyArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await _guard(
            "request_login_otp",
            lambda: service.request_login_otp(
                email=email,
                phone=phone,
                otp_channel=otp_channel,
                locale=locale,
                currency=currency,
                ctx=ctx,
            ),
        )

    @server.tool(
        name="resend_login_otp",
        title="Resend eSIM login code",
        description=(
            "Send the pending one-time code again, to the same destination as the login already "
            "in progress.\n"
            'WHEN: only when the user explicitly asks for another code ("resend it", "I never '
            'got it").\n'
            "Pass the same email or phone that started the login.\n"
            "DO NOT call this on your own after a wrong or expired code, and do not use it to "
            "retry a failed request_login_otp. If the platform reports that a code is still "
            "active or that the limit was reached, tell the user plainly and wait -- do not call "
            "it again."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        ),
    )
    async def resend_login_otp(
        email: EmailArg = None,
        phone: PhoneArg = None,
        otp_channel: ChannelArg = None,
        locale: LocaleArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await _guard(
            "resend_login_otp",
            lambda: service.resend_login_otp(email=email, phone=phone, otp_channel=otp_channel, locale=locale, ctx=ctx),
        )

    @server.tool(
        name="verify_login_otp",
        title="Verify eSIM login code",
        description=(
            "Finish login: verify the six-digit code the user received and open their eSIM "
            "session.\n"
            "WHEN: the user has given you a six-digit code for a login you started with "
            "request_login_otp.\n"
            "Pass that code together with the same email or phone the code was sent to.\n"
            "AFTER SUCCESS (status 'authenticated'): tell the user they are signed in. The "
            "session is kept on this server for this client; you never see, need or handle any "
            "token.\n"
            "DO NOT ask the user for an access token, a refresh token or a password, and do not "
            "retry a rejected code by yourself -- ask the user to re-read it, or to ask for a new "
            "one."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        ),
    )
    async def verify_login_otp(
        verification_pin: PinArg,
        email: EmailArg = None,
        phone: PhoneArg = None,
        locale: LocaleArg = None,
        currency: CurrencyArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await _guard(
            "verify_login_otp",
            lambda: service.verify_login_otp(
                verification_pin=verification_pin,
                email=email,
                phone=phone,
                locale=locale,
                currency=currency,
                ctx=ctx,
            ),
        )

    @server.tool(
        name="get_login_status",
        title="Check eSIM login status",
        description=(
            "Check whether this client already has a signed-in eSIM user. Local and fast: it "
            "does not call the eSIM platform.\n"
            "WHEN: before anything that needs a signed-in user, and whenever the user asks "
            "whether they are logged in.\n"
            "If 'authenticated' is true, carry on -- do not ask the user to log in again. If the "
            "result shows a pending login, ask the user for the six-digit code instead of "
            "starting a new login."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def get_login_status(ctx: Context | None = None) -> dict[str, Any]:
        return await _guard("get_login_status", lambda: service.get_login_status(ctx=ctx))

    @server.tool(
        name="get_user_profile",
        title="Get eSIM account profile",
        description=(
            "Read the signed-in user's own profile and wallet balance from the eSIM platform.\n"
            "WHEN: the user asks about their account, profile, name or balance -- and only while "
            "they are signed in.\n"
            "If this answers 'authentication_required', start the login flow with "
            "request_login_otp instead of asking the user for any credential.\n"
            "Contact details come back masked; tokens and internal session data are never "
            "included, so do not ask for or expect them.\n"
            "PRIVACY: repeat the masked email and phone exactly as returned. Never restore or "
            "retype the complete address or number, not even when the user typed it earlier in "
            "this conversation -- say 'your email' or the masked form instead."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def get_user_profile(
        locale: LocaleArg = None,
        currency: CurrencyArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await _guard(
            "get_user_profile", lambda: service.get_user_profile(locale=locale, currency=currency, ctx=ctx)
        )

    @server.tool(
        name="logout",
        title="Log out of eSIM",
        description=(
            "Sign the current user out of the eSIM platform, for this client only.\n"
            "WHEN: only when the user explicitly asks to log out or sign out.\n"
            "DO NOT log anyone out on your own -- not after an error, not to 'reset' things, not "
            "at the end of a conversation. Other clients and other users are unaffected."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True),
    )
    async def logout(locale: LocaleArg = None, ctx: Context | None = None) -> dict[str, Any]:
        return await _guard("logout", lambda: service.logout(locale=locale, ctx=ctx))
