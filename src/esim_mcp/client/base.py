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
#: * ``/mcp/wallet/top-up/checkout`` opens a hosted payment page for a wallet top-up. Like
#:   the card checkout it moves nothing: it creates an *unpaid* order and a Stripe-hosted
#:   page, and only the platform's signature-verified webhook can credit the balance. The
#:   legacy ``POST /wallet/top-up`` stays forbidden below and always will be -- it answers
#:   with a payment-intent client secret, which is useless to a chat client and dangerous to
#:   put anywhere near one.
PERMITTED_MUTATION_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/mcp/user/bundle/assign"),
        ("POST", "/mcp/user/bundle/card/checkout"),
        ("POST", "/mcp/wallet/top-up/checkout"),
    }
)

#: **QA-only, and off unless a flag says otherwise.** The platform's *legacy* eSIM top-up
#: route. It is deliberately NOT a member of :data:`PERMITTED_MUTATION_ROUTES`: it is only
#: reachable when :func:`enforce_route_is_permitted` is called with
#: ``qa_esim_topup_enabled=True``, which :class:`BackendApiClient` derives from
#: :attr:`~esim_mcp.settings.Settings.esim_topup_execution_enabled` -- a flag that defaults
#: to off and that production settings refuse to construct.
#:
#: Everything about this route is worse than the routes above, and none of it is fixable
#: from this side: no idempotency key, no request-key column on the row it writes, a wallet
#: debit *before* provisioning, and a swallowed provisioning failure. It is admitted for QA
#: so the flow can be exercised end to end, and for no other reason.
QA_ESIM_TOPUP_ROUTE: tuple[str, str] = ("POST", "/user/bundle/assign-top-up")

#: Reads whose *fixed* path nevertheless contains a forbidden marker, so they have to be
#: named here to be reachable at all. Exact ``(method, path)`` match, never a substring:
#: ``/mcp/wallet/top-up/options`` is permitted and ``/wallet/top-up`` is not, and one is not
#: a prefix of the other by accident.
PERMITTED_EXACT_READ_ROUTES: frozenset[tuple[str, str]] = frozenset({("GET", "/mcp/wallet/top-up/options")})

#: Read-only routes whose trailing path segments are references rather than fixed words, so
#: they cannot be allowlisted by exact match. The prefix is matched exactly and the remainder
#: must be exactly ``segments`` opaque segments: no extra slash, no dot-segment, no query, no
#: marker smuggled in. A path that starts with one of these prefixes and fails that check is
#: refused outright rather than falling through to the marker scan, so a malformed reference
#: can never be rescued by simply not containing a banned word.
#:
#: Each entry is ``(method, prefix, segments)``:
#:
#: * the card payment status, keyed on one payment reference;
#: * the wallet top-up status, keyed on one payment reference;
#: * ``/user/consumption/{iccid}`` -- the platform's own live usage read, which the marker
#:   list would otherwise refuse. It is a **read**: it provisions nothing, tops up nothing
#:   and is scoped to the caller by the backend, which resolves the ICCID against the
#:   authenticated user's own profiles before asking the provider;
#: * ``/user/related-topup/{bundle_code}/{iccid}`` -- the platform's own list of top-up
#:   bundles compatible with one of the caller's eSIMs. Two segments, both validated.
PERMITTED_REFERENCE_READ_ROUTES: frozenset[tuple[str, str, int]] = frozenset(
    {
        ("GET", "/mcp/user/bundle/card/status/", 1),
        ("GET", "/mcp/wallet/top-up/status/", 1),
        ("GET", "/user/consumption/", 1),
        ("GET", "/user/related-topup/", 2),
    }
)

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
    # The whole top-up family is banned and then four members are allowlisted back above,
    # exactly as ``bundle/assign`` is. Both spellings are listed because the platform uses
    # both: ``/user/bundle/assign-top-up`` and ``/user/related-topup/...``. Without them,
    # any *other* top-up route -- ``/mcp/user/bundle/topup``, a future ``/wallet/credit`` --
    # would be reachable simply by not spelling a banned word, since everything here is a
    # denylist. With them, the only top-up paths that resolve are the four named above: the
    # compatibility read, the wallet options read, the wallet checkout and its status read.
    # The route that actually tops a SIM up is not among them and is not implemented.
    "top-up",
    "topup",
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
    def settings(self) -> Settings:
        """The settings this transport was built from.

        Exposed so a client module can size its *own* read budget from configuration without
        being handed a second :class:`~esim_mcp.settings.Settings` at construction, which
        would make it possible for a transport and the module using it to disagree about
        the configuration they are running under.
        """
        return self._settings

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
        read_timeout: float | None = None,
    ) -> Any:
        """Perform a request and return only the envelope's ``data`` payload.

        ``allow_retry`` must stay ``False`` for every mutation: OTP request, OTP resend,
        OTP verification, refresh-token rotation and logout are never replayed.

        ``read_timeout`` overrides the pooled client's read budget for this one call and
        leaves every other caller on the configured default. It exists for reads the platform
        builds per user rather than serves from a cache, which take far longer than the
        cached lookups :attr:`~esim_mcp.settings.Settings.read_timeout` is sized for. A
        widened budget and ``allow_retry=True`` do not belong together: three attempts at a
        two-minute budget is six minutes of a chat client waiting to be told the same thing,
        so a caller that widens this is expected to take a single attempt.

        Fails closed before any I/O when the method or the path is not permitted.
        """
        enforce_route_is_permitted(
            method, path, qa_esim_topup_enabled=self._settings.esim_topup_execution_enabled
        )
        url = self.url_for(path)
        headers = self.build_headers(device_id=device_id, locale=locale, currency=currency, credentials=credentials)
        attempts = _MAX_READ_ATTEMPTS if allow_retry else 1
        last_error: EsimMcpError | None = None

        for attempt in range(attempts):
            started = time.monotonic()
            try:
                response = await self._client.request(
                    method.upper(),
                    url,
                    json=json_body,
                    params=params,
                    headers=headers,
                    **self._timeout_override(read_timeout),
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
        read_timeout: float | None = None,
    ) -> BackendOutcome:
        """Send exactly one request and report what came back, without raising on failure.

        ``read_timeout`` widens the pooled client's read budget for this one call. It exists
        for the card checkout, whose endpoint does far more work than anything else this
        server calls: abandoning that request early does not prevent the work, it only
        discards the answer, and the answer is the payment link.

        There is **no retry here, by construction**: this is the path a purchase takes, and a
        repeated send is only ever safe when the caller decides to make it, with the same
        idempotency key it used the first time. A timeout or a dropped connection raises
        :class:`~esim_mcp.errors.BackendTimeoutError` /
        :class:`~esim_mcp.errors.BackendUnavailableError` so the caller can treat the outcome
        as *unknown* rather than as a failure; every answer the server actually gave is
        returned as a :class:`BackendOutcome`, failures included.

        Fails closed before any I/O when the method or the path is not permitted.
        """
        enforce_route_is_permitted(
            method, path, qa_esim_topup_enabled=self._settings.esim_topup_execution_enabled
        )
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
            response = await self._client.request(
                method.upper(), url, json=json_body, headers=headers, **self._timeout_override(read_timeout)
            )
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

    def _timeout_override(self, read_timeout: float | None) -> dict[str, Any]:
        """Per-request timeout kwargs, or nothing when the pooled default applies.

        Only the *read* budget moves. Connect, write and pool stay as configured, because a
        slow endpoint is slow while it thinks, not while it accepts a connection.
        """
        if read_timeout is None:
            return {}
        return {
            "timeout": httpx.Timeout(
                connect=self._settings.connect_timeout,
                read=read_timeout,
                write=self._settings.write_timeout,
                pool=self._settings.pool_timeout,
            )
        }

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


def enforce_route_is_permitted(method: str, path: str, *, qa_esim_topup_enabled: bool = False) -> None:
    """Raise unless ``method`` and ``path`` are ones this server is allowed to call.

    ``qa_esim_topup_enabled`` admits exactly one extra route,
    :data:`QA_ESIM_TOPUP_ROUTE`, and defaults to ``False`` so that every caller that does not
    deliberately opt in -- including every existing one -- keeps the stricter behaviour. With
    the flag off the legacy top-up route is refused here, before any I/O, exactly as it
    always was.

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
    if (normalized_method, normalized_path) in PERMITTED_EXACT_READ_ROUTES:
        return
    if qa_esim_topup_enabled and (normalized_method, normalized_path) == QA_ESIM_TOPUP_ROUTE:
        # Exact whole-path match, like every other allowlist entry, so no neighbour of this
        # route (``/user/bundle/assign``, ``/mcp/user/bundle/assign-top-up``, anything with a
        # suffix) is admitted alongside it.
        logger.warning("qa_esim_topup_route_permitted", extra={"http_path": normalized_path})
        return

    for allowed_method, prefix, segments in PERMITTED_REFERENCE_READ_ROUTES:
        if not normalized_path.startswith(prefix):
            continue
        # Reaching the prefix at all decides the outcome here: a wrong method, or a
        # remainder that is not exactly ``segments`` plain segments, is refused and never
        # handed on to the marker scan.
        if normalized_method == allowed_method and _reference_segments_ok(normalized_path[len(prefix) :], segments):
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


def _reference_segments_ok(remainder: str, expected: int) -> bool:
    """True when ``remainder`` is exactly ``expected`` plain, opaque path segments.

    Split rather than regex-matched across the whole remainder, so ``a/b`` cannot satisfy a
    one-segment route and ``a`` cannot satisfy a two-segment one. An empty part -- which is
    what a leading, trailing or doubled slash produces -- fails the segment pattern and
    therefore fails the whole check.
    """
    parts = remainder.split("/")
    if len(parts) != expected:
        return False
    return all(_REFERENCE_SEGMENT_RE.match(part) for part in parts)


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
