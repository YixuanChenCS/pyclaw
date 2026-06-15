from __future__ import annotations

from pathlib import Path
import tempfile

import pytest
from fastapi.testclient import TestClient

from apps._local_support import NoopObservabilityService
from apps.api import create_app, create_platform_api
from packages.shared_types import HealthCheckResult
from services.agent_core import LocalAgentCoreService
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class _StubPlatformAPI:
    def __init__(self, health: HealthCheckResult) -> None:
        self._health = health

    async def get_health(self) -> HealthCheckResult:
        return self._health


class _RepoIntelligence:
    pass


def _platform_health(
    *,
    status: str = "ready",
    runtime_status: str = "ready",
    runtime_details: dict[str, object] | None = None,
    observability_status: str = "ready",
) -> HealthCheckResult:
    return HealthCheckResult(
        service="platform-api",
        status=status,
        details={
            "runtime": {
                "service": "execution-runtime",
                "status": runtime_status,
                "details": runtime_details
                or {
                    "db": "ready",
                    "queue": {"queued": 0},
                    "locks": {"status": "ready", "active_leases": 0},
                    "artifact_store": "ready",
                    "deployment": "ready",
                },
            },
            "observability": {
                "service": "noop-observability",
                "status": observability_status,
                "details": {},
            },
        },
    )


def test_health_route_returns_200_for_healthy_status():
    app = create_app(platform_api=_StubPlatformAPI(_platform_health()))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["details"]["runtime"]["details"]["db"] == "ready"
    assert response.json()["details"]["runtime"]["details"]["queue"]["queued"] == 0


@pytest.mark.parametrize(
    ("component_name", "component_value"),
    [
        ("db", "not_ready"),
        ("runtime", "not_ready"),
        ("queue", {"status": "not_ready", "queued": 3}),
        ("locks", {"status": "not_ready", "active_leases": 1}),
        ("artifact_store", "not_ready"),
    ],
)
def test_health_route_returns_503_for_unhealthy_components(
    component_name: str,
    component_value: object,
):
    runtime_status = "ready"
    runtime_details = {
        "db": "ready",
        "queue": {"queued": 0},
        "locks": {"status": "ready", "active_leases": 0},
        "artifact_store": "ready",
        "deployment": "ready",
    }
    if component_name == "runtime":
        runtime_status = "not_ready"
    else:
        runtime_details[component_name] = component_value

    app = create_app(
        platform_api=_StubPlatformAPI(
            _platform_health(
                status="not_ready",
                runtime_status=runtime_status,
                runtime_details=runtime_details,
            )
        )
    )

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    runtime = response.json()["details"]["runtime"]
    if component_name == "runtime":
        assert runtime["status"] == "not_ready"
    else:
        assert runtime["details"][component_name] == component_value


def test_health_route_reports_missing_deployment_adapter_without_crashing():
    with tempfile.TemporaryDirectory() as tmpdir:
        runtime = LocalExecutionRuntimeService(
            repository=SQLiteExecutionRuntimeRepository(Path(tmpdir) / "runtime.sqlite3"),
        )
        platform_api = create_platform_api(
            agent_core=LocalAgentCoreService(),
            execution_runtime=runtime,
            repo_intelligence=_RepoIntelligence(),
            observability=NoopObservabilityService(),
        )
        app = create_app(platform_api=platform_api)

        with TestClient(app) as client:
            response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["details"]["runtime"]["details"]["deployment"] == "not_configured"

