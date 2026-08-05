"""Structured logging with a correlation id and mandatory redaction.

Every record produced by this process is rendered first and then passed through
:func:`~esim_mcp.safety.redaction.redact_text`, so a token, OTP, email or device id that
leaks into a log call is destroyed before it reaches a handler. Logs go to *stderr*:
stdout belongs to the stdio MCP transport and must stay pure JSON-RPC.
"""

from __future__ import annotations

import contextvars
import json
import logging
import secrets
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from esim_mcp.safety.redaction import redact_mapping, redact_text

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("esim_mcp_correlation_id", default=None)

_LOG_RECORD_BUILTINS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def new_correlation_id() -> str:
    """Generate a fresh correlation id (hex, so digit-based redaction never eats it)."""
    return secrets.token_hex(8)


def get_correlation_id() -> str | None:
    """Correlation id bound to the current request context, if any."""
    return _correlation_id.get()


@contextmanager
def correlation_context(correlation_id: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the duration of a request.

    This is request-local context only -- never session storage.
    """
    value = correlation_id or new_correlation_id()
    token = _correlation_id.set(value)
    try:
        yield value
    finally:
        _correlation_id.reset(token)


class RedactionFilter(logging.Filter):
    """Render the record and redact it in place.

    Applied to handlers rather than loggers so records propagated from third-party
    libraries (httpx, mcp, uvicorn) are covered too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - defensive: bad % formatting
            rendered = str(record.msg)
        record.msg = redact_text(rendered)
        record.args = ()
        for key, value in list(record.__dict__.items()):
            if key not in _LOG_RECORD_BUILTINS:
                # Key-aware: an ``extra`` named ``device_id`` or ``access_token`` is
                # redacted even when its value carries no recognizable pattern.
                record.__dict__[key] = redact_mapping({key: value})[key]
        if record.exc_info:
            # Tracebacks can embed request payloads through exception messages (pydantic
            # echoes offending input), so the trace is rendered, redacted and re-attached
            # as a plain field instead of being formatted later by a handler.
            record.__dict__["exception"] = redact_text("".join(traceback.format_exception(*record.exc_info)).strip())
            record.exc_info = None
            record.exc_text = None
        return True


class JsonLogFormatter(logging.Formatter):
    """Compact JSON formatter including the correlation id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        correlation_id = getattr(record, "correlation_id", None) or get_correlation_id()
        if correlation_id:
            payload["correlation_id"] = correlation_id
        for key, value in record.__dict__.items():
            if key not in _LOG_RECORD_BUILTINS and key != "correlation_id":
                payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Install the stderr handler, the JSON formatter and the redaction filter."""
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        JsonLogFormatter() if json_output else logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s")
    )
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # httpx logs the full request line (including query strings) at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def install_redaction_filter(handler: logging.Handler) -> None:
    """Attach the redaction filter to an externally created handler (e.g. in tests)."""
    handler.addFilter(RedactionFilter())
