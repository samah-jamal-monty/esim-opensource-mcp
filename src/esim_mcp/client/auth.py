"""Typed wrapper over the backend's ``/api/v1/auth`` routes.

Every method here is a mutation except :meth:`AuthApiClient.get_user_info`, so only that
one is allowed to retry. Tokens travel as ``SecretStr`` and are handed to the transport
layer through :class:`RequestCredentials`; they are never accepted from, or returned to,
the MCP tool layer.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import SecretStr, ValidationError

from esim_mcp.client.base import BackendApiClient, RequestCredentials
from esim_mcp.errors import InvalidBackendResponseError
from esim_mcp.models.auth import BackendAuthResponse, LoginRequest, VerifyOtpRequest

logger = logging.getLogger(__name__)

LOGIN_PATH = "/auth/login"
RESEND_OTP_PATH = "/auth/resend-otp"
VERIFY_OTP_PATH = "/auth/verify_otp"
REFRESH_TOKEN_PATH = "/auth/refresh-token"
USER_INFO_PATH = "/auth/user-info"
LOGOUT_PATH = "/auth/logout"


class AuthApiClient:
    """Backend authentication calls, one method per documented route."""

    def __init__(self, client: BackendApiClient) -> None:
        self._client = client

    async def login(self, request: LoginRequest, *, device_id: str, locale: str) -> None:
        """``POST /auth/login`` -- request an OTP. Rate limited: never retried."""
        await self._client.request(
            "POST",
            LOGIN_PATH,
            device_id=device_id,
            json_body=request.to_payload(),
            locale=locale,
            allow_retry=False,
        )

    async def resend_otp(self, request: LoginRequest, *, device_id: str, locale: str) -> None:
        """``POST /auth/resend-otp`` -- resend the active OTP. Never retried."""
        await self._client.request(
            "POST",
            RESEND_OTP_PATH,
            device_id=device_id,
            json_body=request.to_payload(),
            locale=locale,
            allow_retry=False,
        )

    async def verify_otp(
        self,
        request: VerifyOtpRequest,
        *,
        device_id: str,
        locale: str,
        currency: str,
    ) -> BackendAuthResponse:
        """``POST /auth/verify_otp`` -- exchange the OTP for tokens. Never retried."""
        data = await self._client.request(
            "POST",
            VERIFY_OTP_PATH,
            device_id=device_id,
            json_body=request.to_payload(),
            locale=locale,
            currency=currency,
            allow_retry=False,
        )
        return _parse_auth_response(data, require_tokens=True)

    async def refresh_token(
        self,
        *,
        device_id: str,
        refresh_token: SecretStr,
        locale: str,
        currency: str,
    ) -> BackendAuthResponse:
        """``POST /auth/refresh-token`` -- rotate tokens. Never retried."""
        data = await self._client.request(
            "POST",
            REFRESH_TOKEN_PATH,
            device_id=device_id,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(refresh_token=refresh_token),
            allow_retry=False,
        )
        return _parse_auth_response(data, require_tokens=True)

    async def get_user_info(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        locale: str,
        currency: str,
    ) -> BackendAuthResponse:
        """``GET /auth/user-info`` -- a read, so a bounded retry is allowed."""
        data = await self._client.request(
            "GET",
            USER_INFO_PATH,
            device_id=device_id,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
            allow_retry=True,
        )
        return _parse_auth_response(data, require_tokens=False)

    async def logout(self, *, device_id: str, access_token: SecretStr, locale: str) -> None:
        """``POST /auth/logout`` -- mutation, never retried."""
        await self._client.request(
            "POST",
            LOGOUT_PATH,
            device_id=device_id,
            locale=locale,
            credentials=RequestCredentials(access_token=access_token),
            allow_retry=False,
        )


def _parse_auth_response(data: Any, *, require_tokens: bool) -> BackendAuthResponse:
    """Validate the ``data`` portion of an auth envelope."""
    if not isinstance(data, dict):
        raise InvalidBackendResponseError()
    try:
        parsed = BackendAuthResponse.model_validate(data)
    except ValidationError:
        # The raw payload holds tokens; only the fact of the failure is logged.
        logger.warning("auth_response_validation_failed")
        raise InvalidBackendResponseError() from None
    if require_tokens and not parsed.has_session_tokens:
        logger.warning("auth_response_missing_tokens")
        raise InvalidBackendResponseError()
    return parsed
