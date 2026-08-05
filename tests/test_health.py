from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

client = TestClient(app)
HEALTH_URL = f"{get_settings().api_v1_prefix}/health"


def test_health_returns_200() -> None:
    response = client.get(HEALTH_URL)
    assert response.status_code == 200


def test_health_status_is_up() -> None:
    response = client.get(HEALTH_URL)
    assert response.json()["status"] == "UP"


def test_health_payload_shape() -> None:
    payload = client.get(HEALTH_URL).json()
    settings = get_settings()
    assert payload == {
        "status": "UP",
        "service": settings.app_name,
        "version": settings.app_version,
    }


def test_docs_available() -> None:
    assert client.get("/docs").status_code == 200
