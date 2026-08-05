"""Shared boundary between an MCP tool and the services behind it.

Two jobs, applied identically to every tool in every phase:

* bind a correlation id for the duration of the call, so the backend request, the result
  and any warning share one id in the logs;
* make sure only a typed :class:`~esim_mcp.errors.EsimMcpError` can escape. An unexpected
  exception is logged with its (redacted) traceback and replaced by a generic internal
  error, because ``str(exc)`` of an arbitrary exception is rendered straight into the
  model's context by the SDK and may embed request data.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from esim_mcp.errors import EsimMcpError, InternalError
from esim_mcp.logging_config import correlation_context

logger = logging.getLogger(__name__)


async def guarded(name: str, coro_factory: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Run a tool body under a correlation id, allowing only safe errors out."""
    with correlation_context() as correlation_id:
        try:
            return await coro_factory()
        except EsimMcpError:
            raise
        except Exception:
            logger.exception("tool_unhandled_error", extra={"tool": name, "correlation_id": correlation_id})
            raise InternalError() from None
