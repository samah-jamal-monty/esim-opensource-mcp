"""ASGI entry point for the Streamable HTTP deployment.

This module is what a hosting platform runs. It exposes the *same* :class:`MCPServer`
built by :func:`esim_mcp.server.build_components` -- the same tools, the same session
model, the same identity rules -- over Streamable HTTP at exactly ``/mcp``, plus an
unauthenticated ``GET /health`` for platform health checks.

Nothing here invents an authentication surface. The MCP SDK mounts OAuth metadata
(``/.well-known/oauth-protected-resource``) and client-registration routes *only* when an
``AuthSettings``/token-verifier pair is configured on the server; none is configured, so
those routes legitimately stay absent (404) rather than being faked. See the security
model in the README: over Streamable HTTP without OAuth there is no verified principal, so
``production`` fails closed and a deployment that must serve real users needs a token
verifier wired first.

Bind address
------------
:class:`~esim_mcp.settings.Settings` deliberately reads prefixed names only, so a
platform-supplied ``PORT`` cannot silently reconfigure the server. A hosted deployment
still has to listen on the port its platform hands it, so that one fallback is made
*explicit* here instead of in the settings model:

``ESIM_MCP_PORT`` (operator's own choice) > ``PORT`` (platform) > the settings default.

The bind host stays ``ESIM_MCP_HOST``; set it to ``0.0.0.0`` on a hosted platform. It also
decides the SDK's DNS-rebinding protection: a loopback bind keeps Host/Origin validation
on for local development, a public bind leaves it to the platform's own proxy.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from esim_mcp import __version__
from esim_mcp.server import SERVER_NAME, ServerComponents, build_components
from esim_mcp.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: The one path MCP clients connect to. Claude Desktop, Claude Code and the SDK client all
#: expect the remote server URL itself to be this endpoint.
MCP_PATH = "/mcp"

#: Platform health check. Deliberately outside the MCP protocol and unauthenticated.
HEALTH_PATH = "/health"

#: Binds for which the SDK's DNS-rebinding protection is meaningful (local development).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_HOST_ENV = "ESIM_MCP_HOST"
_PORT_ENV = "ESIM_MCP_PORT"
_PLATFORM_PORT_ENV = "PORT"


def resolve_bind_host(settings: Settings, env: Mapping[str, str] | None = None) -> str:
    """The address to listen on: ``ESIM_MCP_HOST``, else the settings default."""
    resolved = settings.host
    environ = os.environ if env is None else env
    if resolved in LOOPBACK_HOSTS and _HOST_ENV not in environ and _PLATFORM_PORT_ENV in environ:
        logger.warning(
            "bind_host_is_loopback_on_a_hosted_platform",
            extra={"bind_host": resolved, "hint": f"set {_HOST_ENV}=0.0.0.0"},
        )
    return resolved


def resolve_bind_port(settings: Settings, env: Mapping[str, str] | None = None) -> int:
    """The port to listen on.

    ``ESIM_MCP_PORT`` wins because it is this server's own, explicit setting. Otherwise a
    platform-supplied ``PORT`` (Render, Heroku, Cloud Run) is honoured -- the port is not a
    security decision, and refusing it would just make the service unreachable.
    """
    environ = os.environ if env is None else env
    if _PORT_ENV in environ:
        return settings.port

    platform_port = environ.get(_PLATFORM_PORT_ENV)
    if platform_port is None:
        return settings.port

    try:
        port = int(platform_port.strip())
    except ValueError as exc:
        raise ValueError(f"{_PLATFORM_PORT_ENV} must be an integer, got {platform_port!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{_PLATFORM_PORT_ENV} must be between 1 and 65535, got {port}")
    return port


def create_app(
    settings: Settings | None = None,
    *,
    components: ServerComponents | None = None,
) -> Starlette:
    """Build the ASGI application serving MCP at ``/mcp`` and health at ``/health``.

    The returned app owns the server's lifecycle: its Starlette lifespan starts the
    Streamable HTTP session manager, which enters the :class:`MCPServer` lifespan once and
    closes the backend HTTP client and the in-memory stores on shutdown.
    """
    resolved_settings = settings or get_settings()
    resolved_components = components or build_components(resolved_settings)
    server = resolved_components.server

    @server.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> Response:
        """Liveness only: no backend call, no session state, nothing about the caller."""
        return JSONResponse({"status": "ok", "service": SERVER_NAME, "version": __version__})

    bind_host = resolve_bind_host(resolved_settings)
    app = server.streamable_http_app(streamable_http_path=MCP_PATH, host=bind_host)
    # Kept reachable for tests and for anything that needs the object graph behind the app.
    app.state.esim_mcp = resolved_components

    logger.info(
        "http_app_created",
        extra={
            "mcp_path": MCP_PATH,
            "health_path": HEALTH_PATH,
            "bind_host": bind_host,
            "environment": resolved_settings.environment.value,
            "dns_rebinding_protection": bind_host in LOOPBACK_HOSTS,
        },
    )
    return app


def serve(settings: Settings | None = None) -> None:
    """Run the ASGI app under uvicorn. Used by ``python -m esim_mcp.server``."""
    import uvicorn

    resolved_settings = settings or get_settings()
    app = create_app(resolved_settings)
    uvicorn.run(
        app,
        host=resolve_bind_host(resolved_settings),
        port=resolve_bind_port(resolved_settings),
        log_level=resolved_settings.log_level.lower(),
    )
