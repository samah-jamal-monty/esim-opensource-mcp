from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import Settings, get_settings


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings: Settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app: FastAPI = create_app()
