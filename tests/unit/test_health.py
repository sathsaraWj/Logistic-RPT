"""Smoke tests for the API scaffold added in Phase 1."""

from __future__ import annotations

from apps.api.main import create_app
from fastapi.testclient import TestClient


def test_health_live() -> None:
    client = TestClient(create_app())
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready() -> None:
    """Readiness reports control-plane DB reachability; this suite runs with no live
    PostgreSQL instance, so "degraded" (not "ok") is the expected, correct outcome here — see
    tests/integration for a real-database check."""
    client = TestClient(create_app())
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["checks"]["control_plane_database"] in {"ok", "unreachable"}


def test_version() -> None:
    client = TestClient(create_app())
    response = client.get("/version")
    body = response.json()
    assert response.status_code == 200
    assert body["service"] == "hermes-rpt"
    assert "version" in body
    assert "uptime_seconds" in body


def test_response_carries_correlation_and_request_ids() -> None:
    client = TestClient(create_app())
    response = client.get("/health/live")
    assert response.headers.get("x-request-id")
    assert response.headers.get("x-correlation-id")


def test_correlation_id_is_echoed_back_when_supplied() -> None:
    client = TestClient(create_app())
    response = client.get("/health/live", headers={"X-Correlation-ID": "caller-supplied-id"})
    assert response.headers.get("x-correlation-id") == "caller-supplied-id"
