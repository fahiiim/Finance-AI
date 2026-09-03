from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _no_live_dependency_calls(monkeypatch):
    """
    Readiness genuinely probes OpenAI and the backend, which is the point of it
    -- and exactly what a test suite must not do. Both caches are primed so no
    test reaches the network; individual tests override them to assert the
    failure paths.
    """
    from app.api.routes import health

    monkeypatch.setattr(health, "_probe_cache", (float("inf"), True, "stubbed reachable"))
    monkeypatch.setattr(health, "_llm_probe_cache", (float("inf"), True, "stubbed key accepted"))


def test_health_returns_ok():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_reports_checks():
    client = TestClient(app)
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert "backend_integration_mode" in body["checks"]


def test_ready_fails_when_the_openai_key_is_rejected(monkeypatch):
    """
    A key that is PRESENT but revoked passes a bool() check and fails every
    single chat request -- the same mistake as the empty backend token, on the
    other dependency. Key rotation makes this an ordinary production event.
    """
    from app.api.routes import health

    monkeypatch.setattr(
        health, "_llm_probe_cache", (float("inf"), False, "key rejected or unreachable (AuthenticationError)")
    )
    response = TestClient(app).get("/ready")
    assert response.status_code == 503
    assert any("OpenAI" in p for p in response.json()["problems"])
    assert response.json()["checks"]["openai_key_valid"] is False
    assert response.json()["checks"]["openai_api_key_configured"] is True, (
        "configured-but-invalid must be distinguishable from missing"
    )


def test_ready_fails_when_the_backend_token_is_missing(monkeypatch):
    """
    The failure this endpoint exists for. With an empty service token the AI
    answers "your figures are temporarily unavailable" to every question, and
    /ready used to report 200 "ready" throughout -- so nothing was monitoring
    the one thing that was broken.
    """
    from app.api.routes import health
    from app.core.config import BackendIntegrationMode

    monkeypatch.setattr(health.settings, "backend_integration_mode", BackendIntegrationMode.BACKEND_API)
    monkeypatch.setattr(health.settings, "backend_api_base_url", "https://backend.internal")
    monkeypatch.setattr(health.settings, "backend_api_service_token", "")
    monkeypatch.setattr(health, "_probe_cache", (float("inf"), True, "stubbed reachable"))

    response = TestClient(app).get("/ready")
    assert response.status_code == 503, "a readiness probe must FAIL, not report a string"
    body = response.json()
    assert body["status"] == "not_ready"
    assert any("BACKEND_API_SERVICE_TOKEN" in p for p in body["problems"])
    assert body["checks"]["backend_credentials_configured"] is False


def test_ready_fails_when_the_backend_cannot_be_reached(monkeypatch):
    from app.api.routes import health
    from app.core.config import BackendIntegrationMode

    monkeypatch.setattr(health.settings, "backend_integration_mode", BackendIntegrationMode.BACKEND_API)
    monkeypatch.setattr(health.settings, "backend_api_base_url", "https://backend.internal")
    monkeypatch.setattr(health.settings, "backend_api_service_token", "a-token")
    monkeypatch.setattr(health, "_probe_cache", (float("inf"), False, "unreachable (ConnectError)"))

    response = TestClient(app).get("/ready")
    assert response.status_code == 503
    assert any("unreachable" in p for p in response.json()["problems"])


def test_liveness_never_depends_on_the_backend(monkeypatch):
    """
    /health failing means "restart me". Tying it to a slow backend turns one
    backend incident into a fleet-wide restart loop that fixes nothing.
    """
    from app.api.routes import health
    from app.core.config import BackendIntegrationMode

    monkeypatch.setattr(health.settings, "backend_integration_mode", BackendIntegrationMode.BACKEND_API)
    monkeypatch.setattr(health.settings, "backend_api_base_url", "https://unreachable.invalid")
    monkeypatch.setattr(health.settings, "backend_api_service_token", "")

    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_openapi_docs_available():
    client = TestClient(app)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/api/v1/chat" in schema["paths"]
