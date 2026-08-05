from fastapi import APIRouter

from app.core.config import Settings, get_settings
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Service health check")
def read_health() -> HealthResponse:
    """Return the current service status."""
    settings: Settings = get_settings()
    return HealthResponse(
        status="UP",
        service=settings.app_name,
        version=settings.app_version,
    )
