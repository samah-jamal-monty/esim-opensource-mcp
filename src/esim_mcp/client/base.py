"""Pooled async HTTP client for the eSIM backend.

Responsibilities kept in this layer:

* one pooled :class:`httpx.AsyncClient` with explicit connect/read/write/pool timeouts;
* header assembly (``X-Device-Id``, ``Accept-Language``, ``X-Currency`` and the
  internal-only ``Authorization`` / ``X-Refresh-Token``);
* envelope parsing -- callers receive the ``data`` portion and nothing else;
* translation of transport and envelope failures into typed application errors.

Bodies are never logged, at any level. ``developerMessage`` is used only for internal
classification and never reaches an MCP client.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import SecretStr, ValidationError

from esim_mcp.errors import (
    AuthenticationRequiredError,
    BackendTimeoutError,
    BackendUnavailableError,
    EsimMcpError,
    ExpiredOtpError,
    ForbiddenBackendRouteError,
    InternalError,
    InvalidBackendResponseError,
    InvalidInputError,
    InvalidOtpError,
    OtpLimitReachedError,
    OtpStillActiveError,
    OtpTooFrequentError,
    RateLimitedError,
)
from esim_mcp.logging_config import get_correlation_id
from esim_mcp.models.common import BackendEnvelope
from esim_mcp.settings import Settings

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({502, 503, 504})
_MAX_READ_ATTEMPTS = 3
_INITIAL_BACKOFF_SECONDS = 0.2

#: The *only* mutating routes this server may call, matched as exact normalized paths and
#: exact methods. Everything else that creates an order or moves money stays unreachable.
#:
#: The legacy ``POST /user/bundle/assign`` remains forbidden below and always will be: it has
#: no idempotency key and no duplicate-order protection, so a retried call there can buy a
#: plan twice. The MCP-only routes allowed here are different endpoints with different
#: guarantees -- each requires an ``Idempotency-Key`` and replays a stored answer instead of
#: executing a second time. An exact-match allowlist is used rather than a relaxed marker so
#: that no *other* path containing "bundle/assign" (the legacy one, "assign-top-up", a future
#: variant) can slip through with them.
#:
#: Two entries, and they are not equivalent in severity:
#:
#: * ``/mcp/user/bundle/assign`` debits the wallet, so calling it spends the user's money;
#: * ``/mcp/user/bundle/card/checkout`` opens a hosted payment page and moves nothing. The
#:   card itself is entered on the payment provider's own page, never here -- this server
#:   never sees a card number and cannot capture a payment.
PERMITTED_MUTATION_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/mcp/user/bundle/assign"),
        ("POST", "/mcp/user/bundle/card/checkout"),
    }
)

#: Read-only MCP routes whose last path segment is a reference rather than a fixed word, so
#: they cannot be allowlisted by exact match. The prefix is matched exactly and the remainder
#: must be **one** opaque segment: no slash, no dot-segment, no query, no marker smuggled in.
#: A path that starts with one of these prefixes and fails that check is refused outright
#: rather than falling through to the marker scan, so a malformed reference can never be
#: rescued by simply not containing a banned word.
PERMITTED_REFERENCE_READ_ROUTES: frozenset[tuple[str, str]] = frozenset({("GET", "/mcp/user/bundle/card/status/")})

#: The shape a reference may take inside a permitted path. Deliberately narrower than the URL
#: spec allows: this server generates none of these values, so anything unusual in one came
#: from the backend or from a caller and is not worth being liberal about.
_REFERENCE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")

#: Backend routes this server must never call, matched as substrings of the request path.
#:
#: Everything here creates an order, moves money, or touches a provisioned eSIM.
#: :meth:`BackendApiClient.request` refuses these paths outright, so the rule holds for *any*
#: future caller rather than depending on each client module remembering it.
#: ``/wallet/user_wallet_by_user`` (a read) is deliberately not matched by ``wallet/top-up``
#: or ``user_wallet_by_id``.
FORBIDDEN_PATH_MARKERS: tuple[str, ...] = (
    "bundle/assign",
    # The whole card family is banned and then two members are allowlisted back, exactly as
    # ``bundle/assign`` is. Without this marker a *capture*, a *refund* or a future
    # ``card/confirm`` route would be reachable simply by not spelling a banned word, since
    # everything below is a denylist. With it, reaching any card route other than the two
    # named above is impossible.
    "bundle/card",
    "verify_order_otp",
    "wallet/top-up",
    "user_wallet_by_id",
    "voucher",
    "promotion",
    "promo",
    "callback",
    "payment",
    "stripe",
    "order/",
    "consumption",
    "translate_bundle",
    "translate_tag",
)

#: HTTP methods allowed at all. Everything this server does is a ``GET`` or one of the five
#: documented authentication ``POST``s; nothing needs ``PUT``, ``PATCH`` or ``DELETE``.
ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "POST"})


@dataclass(frozen=True)
class RequestCredentials:
    """Secrets attached to a single request. Never returned to MCP clients."""

    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None


@dataclass(frozen=True)
class BackendOutcome:
    """What a single, un-retried request actually produced.

    Returned instead of a typed error by :meth:`BackendApiClient.request_once`, because for a
    request that may have moved money the *caller* has to classify the answer: a failure
    envelope from a purchase carries the order id and the platform's own next action, and
    collapsing it into an exception at this layer would throw both away.

    ``parsed`` is ``False`` when the response body was not a readable envelope at all. That is
    not the same as a failure: for a mutation it means the outcome is unknown.
    """

    status_code: int
    envelope: BackendEnvelope | None = None
    parsed: bool = False

    @property
    def is_success(self) -> bool:
        """True only when the transport *and* the envelope both report success."""
        return 200 <= self.status_code < 300 and self.envelope is not None and self.envelope.is_success

    @property
    def data(self) -> Any:
        return self.envelope.data if self.envelope is not None else None

    @property
    def error_hint(self) -> str:
        """Backend error label, for internal classification only. Never returned to a client."""
        return self.envelope.error_hint if self.envelope is not None else ""

    @property
    def effective_status(self) -> int:
        """The envelope's own code when it reports a failure, else the transport status."""
        code = self.envelope.response_code if self.envelope is not None else None
        if code is not None and not 200 <= code < 300:
            return code
        return self.status_code


class BackendApiClient:
    """Thin, reusable transport over the backend's ``/api/v1`` surface."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or self._build_client(settings)

    @staticmethod
    def _build_client(settings: Settings) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.write_timeout,
            pool=settings.pool_timeout,
        )
        return httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
            headers={"Accept": "application/json", "User-Agent": "esim-mcp/0.1"},
            follow_redirects=False,
        )

    @property
    def base_url(self) -> str:
        """Normalized backend base URL including the ``/api/v1`` prefix."""
        return self._settings.api_v1_url

    def url_for(self, path: str) -> str:
        """Absolute URL for a backend path such as ``/auth/login``."""
        return f"{self.base_url}/{path.lstrip('/')}"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release pooled connections. Called from the server lifespan."""
        if self._owns_client:
            await self._client.aclose()

    def build_headers(
        self,
        *,
        device_id: str,
        locale: str | None = None,
        currency: str | None = None,
        credentials: RequestCredentials | None = None,
        idempotency_key: SecretStr | None = None,
    ) -> dict[str, str]:
        """Assemble request headers. Secret headers are set here and nowhere else.

        ``idempotency_key`` is an explicit parameter rather than part of a free-form header
        mapping so that the one place a purchase key can be attached is auditable, and so no
        caller can smuggle an ``Authorization`` header of its own past this method.
        """
        headers: dict[str, str] = {
            "X-Device-Id": device_id,
            "Accept-Language": locale or self._settings.default_locale,
        }
        if currency:
            headers["X-Currency"] = currency
        if credentials and credentials.access_token:
            headers["Authorization"] = f"Bearer {credentials.access_token.get_secret_value()}"
        if credentials and credentials.refresh_token:
            headers["X-Refresh-Token"] = credentials.refresh_token.get_secret_value()
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key.get_secret_value()
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        device_id: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        locale: str | None = None,
        currency: str | None = None,
        credentials: RequestCredentials | None = None,
        allow_retry: bool = False,
    ) -> Any:
        """Perform a request and return only the envelope's ``data`` payload.

        ``allow_retry`` must stay ``False`` for every mutation: OTP request, OTP resend,
        OTP verification, refresh-token rotation and logout are never replayed.

        Fails closed before any I/O when the method or the path is not permitted.
        """
        enforce_route_is_permitted(method, path)
        url = self.url_for(path)
        headers = self.build_headers(device_id=device_id, locale=locale, currency=currency, credentials=credentials)
        attempts = _MAX_READ_ATTEMPTS if allow_retry else 1
        last_error: EsimMcpError | None = None

        for attempt in range(attempts):
            started = time.monotonic()
            try:
                response = await self._client.request(
                    method.upper(), url, json=json_body, params=params, headers=headers
                )
            except httpx.TimeoutException:
                last_error = BackendTimeoutError()
            except httpx.HTTPError:
                last_error = BackendUnavailableError()
            else:
                duration_ms = round((time.monotonic() - started) * 1000, 1)
                logger.info(
                    "backend_call",
                    extra={
                        "http_method": method.upper(),
                        "http_path": f"/api/v1/{path.lstrip('/')}",
                        "http_status": response.status_code,
                        "duration_ms": duration_ms,
                        "correlation_id": get_correlation_id(),
                        "attempt": attempt + 1,
                    },
                )
                if response.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                    last_error = BackendUnavailableError()
                else:
                    return self._parse(response)

            if attempt + 1 < attempts:
                await asyncio.sleep(_INITIAL_BACKOFF_SECONDS * (2**attempt))

        raise last_error or InternalError()

    async def request_once(
        self,
        method: str,
        path: str,
        *,
        device_id: str,
        json_body: dict[str, Any] | None = None,
        locale: str | None = None,
        currency: str | None = None,
        credentials: RequestCredentials | None = None,
        idempotency_key: SecretStr | None = None,
    ) -> BackendOutcome:
        """Send exactly one request and report what came back, without raising on failure.

        There is **no retry here, by construction**: this is the path a purchase takes, and a
        repeated send is only ever safe when the caller decides to make it, with the same
        idempotency key it used the first time. A timeout or a dropped connection raises
        :class:`~esim_mcp.errors.BackendTimeoutError` /
        :class:`~esim_mcp.errors.BackendUnavailableError` so the caller can treat the outcome
        as *unknown* rather than as a failure; every answer the server actually gave is
        returned as a :class:`BackendOutcome`, failures included.

        Fails closed before any I/O when the method or the path is not permitted.
        """
        enforce_route_is_permitted(method, path)
        url = self.url_for(path)
        headers = self.build_headers(
            device_id=device_id,
            locale=locale,
            currency=currency,
            credentials=credentials,
            idempotency_key=idempotency_key,
        )
        started = time.monotonic()
        try:
            response = await self._client.request(method.upper(), url, json=json_body, headers=headers)
        except httpx.TimeoutException:
            logger.warning(
                "backend_call_timeout",
                extra={
                    "http_method": method.upper(),
                    "http_path": f"/api/v1/{path.lstrip('/')}",
                    "correlation_id": get_correlation_id(),
                },
            )
            raise BackendTimeoutError() from None
        except httpx.HTTPError:
            logger.warning(
                "backend_call_transport_error",
                extra={
                    "http_method": method.upper(),
                    "http_path": f"/api/v1/{path.lstrip('/')}",
                    "correlation_id": get_correlation_id(),
                },
            )
            raise BackendUnavailableError() from None

        logger.info(
            "backend_call",
            extra={
                "http_method": method.upper(),
                "http_path": f"/api/v1/{path.lstrip('/')}",
                "http_status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "correlation_id": get_correlation_id(),
                "attempt": 1,
            },
        )
        return _outcome_of(response)

    def _parse(self, response: httpx.Response) -> Any:
        """Validate the envelope and return its ``data``, or raise a typed error."""
        if response.status_code == 204 or not response.content:
            if response.is_success:
                return None
            raise classify_backend_error(response.status_code, None)

        try:
            payload = response.json()
        except ValueError:
            logger.warning(
                "backend_response_not_json",
                extra={"http_status": response.status_code, "correlation_id": get_correlation_id()},
            )
            raise InvalidBackendResponseError() from None

        if not isinstance(payload, dict):
            raise InvalidBackendResponseError()

        try:
            envelope = BackendEnvelope.model_validate(payload)
        except ValidationError:
            raise InvalidBackendResponseError() from None

        if not response.is_success or not envelope.is_success:
            logger.warning(
                "backend_call_failed",
                extra={
                    "http_status": response.status_code,
                    "envelope_status": envelope.status,
                    "envelope_response_code": envelope.response_code,
                    "correlation_id": get_correlation_id(),
                },
            )
            raise classify_backend_error(response.status_code, envelope)

        return envelope.data


def _outcome_of(response: httpx.Response) -> BackendOutcome:
    """Turn one response into a :class:`BackendOutcome` without ever raising.

    An unreadable body is reported as ``parsed=False`` rather than as an error, because for a
    request that may have moved money "the server answered something I cannot read" and "the
    server refused" are different facts, and only the caller knows which one is dangerous.
    """
    if not response.content:
        return BackendOutcome(status_code=response.status_code, envelope=None, parsed=False)
    try:
        payload = response.json()
    except ValueError:
        logger.warning(
            "backend_response_not_json",
            extra={"http_status": response.status_code, "correlation_id": get_correlation_id()},
        )
        return BackendOutcome(status_code=response.status_code, envelope=None, parsed=False)
    if not isinstance(payload, dict):
        return BackendOutcome(status_code=response.status_code, envelope=None, parsed=False)
    try:
        envelope = BackendEnvelope.model_validate(payload)
    except ValidationError:
        return BackendOutcome(status_code=response.status_code, envelope=None, parsed=False)
    return BackendOutcome(status_code=response.status_code, envelope=envelope, parsed=True)


def enforce_route_is_permitted(method: str, path: str) -> None:
    """Raise unless ``method`` and ``path`` are ones this server is allowed to call.

    Deliberately at the transport boundary: a new client module, or a future phase, cannot
    reach an order, payment, voucher, provisioning or top-up route even by accident. The
    rejected path is logged (it carries no secret) so the mistake is diagnosable.

    Only the mutating routes named in :data:`PERMITTED_MUTATION_ROUTES` are allowed through,
    by exact ``(method, path)`` match. The allowlist is checked first *because* the marker
    list below would otherwise match them: that ordering is the whole point, and it is why
    the allowlist compares whole paths instead of substrings.
    """
    normalized_method = method.strip().upper()
    if normalized_method not in ALLOWED_METHODS:
        logger.error("forbidden_backend_method", extra={"http_method": normalized_method})
        raise ForbiddenBackendRouteError()

    normalized_path = f"/{path.strip().lstrip('/')}".lower()
    if (normalized_method, normalized_path) in PERMITTED_MUTATION_ROUTES:
        return

    for allowed_method, prefix in PERMITTED_REFERENCE_READ_ROUTES:
        if not normalized_path.startswith(prefix):
            continue
        # Reaching the prefix at all decides the outcome here: a wrong method or a reference
        # that is not one plain segment is refused, never handed on to the marker scan.
        if normalized_method == allowed_method and _REFERENCE_SEGMENT_RE.match(normalized_path[len(prefix) :]):
            return
        logger.error(
            "forbidden_backend_reference_route",
            extra={"http_method": normalized_method, "http_path": prefix},
        )
        raise ForbiddenBackendRouteError()

    for marker in FORBIDDEN_PATH_MARKERS:
        if marker in normalized_path:
            logger.error(
                "forbidden_backend_route",
                extra={"http_method": normalized_method, "http_path": normalized_path, "marker": marker},
            )
            raise ForbiddenBackendRouteError()


_OTP_ERROR_MATCHERS: tuple[tuple[tuple[str, ...], type[EsimMcpError]], ...] = (
    (("otp_request_too_frequent", "too frequent"), OtpTooFrequentError),
    (("otp_still_active", "active otp already exists"), OtpStillActiveError),
    (("otp_limit_reached", "maximum otp requests"), OtpLimitReachedError),
    (("otp_expired", "otp has expired"), ExpiredOtpError),
    (("otp_invalid", "otp provided is invalid", "otp invalid"), InvalidOtpError),
)


def classify_backend_error(status_code: int, envelope: BackendEnvelope | None) -> EsimMcpError:
    """Translate a backend failure into a typed error carrying an MCP-safe message.

    The envelope has no machine-readable error code: ``title`` is a *localized* string
    that falls back to the backend's internal error key when no translation exists. Both
    forms are matched here; neither is ever forwarded to the client.
    """
    hint = (envelope.error_hint if envelope else "").lower()
    envelope_code = envelope.response_code if envelope else None
    # When the two disagree, the signal that reports a failure wins, and the envelope's
    # own code wins over the transport status because it is the more specific one.
    if envelope_code is not None and not 200 <= envelope_code < 300:
        effective_status = envelope_code
    elif not 200 <= status_code < 300:
        effective_status = status_code
    else:
        effective_status = envelope_code or status_code

    for needles, error_type in _OTP_ERROR_MATCHERS:
        if any(needle in hint for needle in needles):
            return error_type()

    if effective_status in (401, 403):
        return AuthenticationRequiredError()
    if effective_status == 429:
        return RateLimitedError()
    if effective_status in (400, 404, 409, 422):
        return InvalidInputError()
    if effective_status >= 500:
        return BackendUnavailableError()
    return InternalError()
