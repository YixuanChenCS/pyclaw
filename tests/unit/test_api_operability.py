from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient

from apps.api import APIConfig, create_app
from packages.shared_types import HealthCheckResult


class _OperabilityPlatformAPI:
    async def get_health(self) -> HealthCheckResult:
        return HealthCheckResult(
            service="platform-api",
            status="ready",
            details={
                "runtime": {
                    "service": "execution-runtime",
                    "status": "ready",
                    "details": {
                        "queue": {"queued": 2, "needs_recovery": 1},
                        "queue_depth": 2,
                        "stale_run_count": 1,
                        "needs_recovery_count": 1,
                        "active_lease_count": 3,
                    },
                }
            },
        )

    async def get_run(self, run_id: str):
        return None

    async def get_operability_snapshot(self) -> dict[str, object]:
        return {
            "service": "platform-api",
            "status": "ready",
            "runtime": {
                "status": "ready",
                "queue": {"queued": 2, "needs_recovery": 1},
                "queue_depth": 2,
                "stale_run_count": 1,
                "needs_recovery_count": 1,
                "active_lease_count": 3,
            },
        }


def test_request_id_is_generated_for_successful_requests():
    app = create_app(platform_api=_OperabilityPlatformAPI())

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


def test_request_id_is_preserved_and_metrics_are_recorded():
    app = create_app(platform_api=_OperabilityPlatformAPI())

    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "req-123"})

    snapshot = app.state.api_metrics.snapshot()
    assert response.headers["X-Request-ID"] == "req-123"
    assert snapshot == {
        "requests_total": 1,
        "duration_ms_total": snapshot["duration_ms_total"],
        "request_counts": [
            {
                "method": "GET",
                "route": "/health",
                "status_code": 200,
                "count": 1,
            }
        ],
    }


def test_access_log_is_structured_json(caplog):
    app = create_app(platform_api=_OperabilityPlatformAPI())

    with caplog.at_level(logging.INFO, logger="pyclaw.api.access"):
        with TestClient(app) as client:
            response = client.get("/runs/run_missing", headers={"X-Request-ID": "req-456"})

    assert response.status_code == 404
    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "http_request"
    assert payload["request_id"] == "req-456"
    assert payload["method"] == "GET"
    assert payload["path"] == "/runs/run_missing"
    assert payload["route"] == "/runs/{run_id}"
    assert payload["status_code"] == 404


def test_unauthorized_requests_still_get_request_id_and_metrics():
    app = create_app(
        platform_api=_OperabilityPlatformAPI(),
        config=APIConfig(api_token="secret-token"),
    )

    with TestClient(app) as client:
        response = client.get("/runs")

    assert response.status_code == 401
    assert response.headers["X-Request-ID"]
    assert app.state.api_metrics.snapshot()["request_counts"] == [
        {
            "method": "GET",
            "route": "/runs",
            "status_code": 401,
            "count": 1,
        }
    ]


def test_metrics_endpoint_returns_prometheus_text():
    app = create_app(platform_api=_OperabilityPlatformAPI())

    with TestClient(app) as client:
        client.get("/health", headers={"X-Request-ID": "req-health"})
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# HELP pyclaw_api_http_requests_total" in response.text
    assert 'pyclaw_api_http_requests_by_route_total{method="GET",route="/health",status_code="200"} 1' in response.text


def test_metrics_endpoint_is_public_when_api_token_is_enabled():
    app = create_app(
        platform_api=_OperabilityPlatformAPI(),
        config=APIConfig(api_token="secret-token"),
    )

    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "# TYPE pyclaw_api_http_requests_total counter" in response.text


def test_runtime_metrics_endpoint_returns_prometheus_text():
    app = create_app(platform_api=_OperabilityPlatformAPI())

    with TestClient(app) as client:
        response = client.get("/metrics/runtime")

    assert response.status_code == 200
    assert "# TYPE pyclaw_runtime_queue_depth gauge" in response.text
    assert "pyclaw_runtime_queue_depth 2" in response.text
    assert 'pyclaw_runtime_runs_by_status{status="needs_recovery"} 1' in response.text


def test_runtime_metrics_endpoint_is_public_when_api_token_is_enabled():
    app = create_app(
        platform_api=_OperabilityPlatformAPI(),
        config=APIConfig(api_token="secret-token"),
    )

    with TestClient(app) as client:
        response = client.get("/metrics/runtime")

    assert response.status_code == 200
    assert "pyclaw_runtime_active_lease_count 3" in response.text
