from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api import APIConfig, create_app, load_api_config_from_env, load_runtime_config_from_env
from packages._python_compat import require_supported_python
from packages.shared_types import ErrorCode, ErrorCodeContractError, HealthCheckResult, RunResult


class _ConfigPlatformAPI:
    async def get_health(self) -> HealthCheckResult:
        return HealthCheckResult(service="platform-api", status="ready")

    async def list_runs(self, workspace_id: str | None = None, *, session_id=None, status=None, limit=None):
        return ()

    async def get_run(self, run_id: str) -> RunResult | None:
        return None

    async def get_artifact(self, artifact_id: str):
        return None

    async def decide_approval(self, approval_id: str, *, approved: bool, comment: str | None = None):
        raise ErrorCodeContractError(
            ErrorCode.APPROVAL_EXPIRED,
            f"Approval {approval_id} expired at 2026-06-15T00:00:00Z.",
        )

    async def trigger_deployment(self, request):
        raise ErrorCodeContractError(
            ErrorCode.DEPLOYMENT_UNAVAILABLE,
            "No deployment adapter is configured.",
        )


def test_load_api_config_from_env_parses_bearer_token_cors_and_workspace_roots(tmp_path: Path):
    first_root = tmp_path / "repo-a"
    second_root = tmp_path / "repo-b"
    first_root.mkdir()
    second_root.mkdir()

    config = load_api_config_from_env(
        {
            "PYCLAW_API_BEARER_TOKEN": " secret-token ",
            "PYCLAW_API_ALLOWED_ORIGINS": "http://localhost:3000, https://example.com ",
            "PYCLAW_API_WORKSPACE_ROOTS": f"{first_root}, {second_root}",
        }
    )

    assert config.api_token == "secret-token"
    assert config.cors_allowed_origins == ("http://localhost:3000", "https://example.com")
    assert config.allowed_workspace_roots == (str(first_root.resolve()), str(second_root.resolve()))


def test_load_api_config_from_env_defaults_workspace_root_to_cwd(tmp_path: Path):
    config = load_api_config_from_env({}, cwd=tmp_path)

    assert config.allowed_workspace_roots == (str(tmp_path.resolve()),)


def test_load_api_config_from_env_rejects_invalid_workspace_root(tmp_path: Path):
    missing_root = tmp_path / "missing"

    try:
        load_api_config_from_env({"PYCLAW_API_WORKSPACE_ROOTS": str(missing_root)})
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected invalid workspace root to raise ValueError")

    assert "PYCLAW_API_WORKSPACE_ROOTS" in message
    assert str(missing_root) in message


def test_load_runtime_config_from_env_prefers_api_db_path(tmp_path: Path):
    db_path = tmp_path / "api-runtime.sqlite3"

    config = load_runtime_config_from_env({"PYCLAW_API_DB_PATH": str(db_path)})

    assert config.runtime_db_path == str(db_path)


def test_create_app_explicit_injection_still_works(tmp_path: Path):
    platform_api = _ConfigPlatformAPI()
    config = APIConfig(api_token="secret-token", allowed_workspace_roots=(str(tmp_path),))

    app = create_app(platform_api=platform_api, config=config)

    assert app.state.platform_api is platform_api
    assert app.state.api_config == config


def test_auth_errors_use_stable_error_envelope():
    app = create_app(platform_api=_ConfigPlatformAPI(), config=APIConfig(api_token="secret-token"))

    with TestClient(app) as client:
        response = client.get("/runs")

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Missing or invalid bearer token."}
    }


def test_run_not_found_uses_stable_error_envelope():
    app = create_app(platform_api=_ConfigPlatformAPI())

    with TestClient(app) as client:
        response = client.get("/runs/run_missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "run not found: run_missing"}
    }


def test_approval_expired_uses_stable_error_envelope():
    app = create_app(platform_api=_ConfigPlatformAPI())

    with TestClient(app) as client:
        response = client.post("/approvals/approval_pending/decision", json={"decision": "approved"})

    assert response.status_code == 410
    assert response.json() == {
        "error": {
            "code": "approval_expired",
            "message": "Approval approval_pending expired at 2026-06-15T00:00:00Z.",
        }
    }


def test_artifact_not_found_uses_stable_error_envelope():
    app = create_app(platform_api=_ConfigPlatformAPI())

    with TestClient(app) as client:
        response = client.get("/artifacts/artifact_missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "artifact not found: artifact_missing"}
    }


def test_deployment_unavailable_uses_stable_error_envelope():
    app = create_app(platform_api=_ConfigPlatformAPI())

    with TestClient(app) as client:
        response = client.post(
            "/deployments",
            json={"run_id": "run_success", "workspace_id": "ws_demo", "target": "staging"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "deployment_unavailable",
            "message": "No deployment adapter is configured.",
        }
    }


def test_openapi_schema_includes_expected_tags_and_error_examples():
    app = create_app(platform_api=_ConfigPlatformAPI())

    schema = app.openapi()

    assert {item["name"] for item in schema["tags"]} >= {
        "health",
        "runs",
        "events",
        "approvals",
        "artifacts",
        "deployments",
    }
    deployment_responses = schema["paths"]["/deployments"]["post"]["responses"]
    assert deployment_responses["503"]["content"]["application/json"]["example"]["error"]["code"] == (
        "deployment_unavailable"
    )
    approval_responses = schema["paths"]["/approvals/{approval_id}/decision"]["post"]["responses"]
    assert approval_responses["410"]["content"]["application/json"]["example"]["error"]["code"] == (
        "approval_expired"
    )
    run_artifact_responses = schema["paths"]["/runs/{run_id}/artifacts"]["get"]["responses"]
    assert run_artifact_responses["404"]["content"]["application/json"]["example"]["error"]["code"] == (
        "not_found"
    )
    approval_list_response = schema["paths"]["/approvals"]["get"]["responses"]["200"]
    approval_detail_response = schema["paths"]["/approvals/{approval_id}"]["get"]["responses"]["200"]
    assert approval_list_response["content"]["application/json"]["schema"]["items"]["$ref"].endswith(
        "/ApprovalRecordResponse"
    )
    assert approval_detail_response["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ApprovalRecordResponse"
    )


def test_openapi_schema_includes_session_id_filter_for_runs():
    app = create_app(platform_api=_ConfigPlatformAPI())

    parameters = app.openapi()["paths"]["/runs"]["get"]["parameters"]

    assert any(item["name"] == "session_id" and item["in"] == "query" for item in parameters)


def test_openapi_schema_documents_sse_as_text_event_stream():
    app = create_app(platform_api=_ConfigPlatformAPI())

    responses = app.openapi()["paths"]["/runs/{run_id}/events/stream"]["get"]["responses"]

    assert "text/event-stream" in responses["200"]["content"]
    assert "application/json" not in responses["200"]["content"]


def test_openapi_validation_errors_use_api_error_response():
    app = create_app(platform_api=_ConfigPlatformAPI())

    schema = app.openapi()
    create_run_422 = schema["paths"]["/runs"]["post"]["responses"]["422"]
    decide_approval_422 = schema["paths"]["/approvals/{approval_id}/decision"]["post"]["responses"]["422"]
    deployment_422 = schema["paths"]["/deployments"]["post"]["responses"]["422"]

    assert create_run_422["content"]["application/json"]["schema"]["$ref"].endswith("/APIErrorResponse")
    assert decide_approval_422["content"]["application/json"]["schema"]["$ref"].endswith("/APIErrorResponse")
    assert deployment_422["content"]["application/json"]["schema"]["$ref"].endswith("/APIErrorResponse")


def test_openapi_error_examples_use_stable_error_envelope():
    app = create_app(platform_api=_ConfigPlatformAPI())

    schema = app.openapi()
    runs_get_404 = schema["paths"]["/runs/{run_id}"]["get"]["responses"]["404"]
    stream_409 = schema["paths"]["/runs/{run_id}/events/stream"]["get"]["responses"]["409"]
    approval_410 = schema["paths"]["/approvals/{approval_id}/decision"]["post"]["responses"]["410"]
    deployment_503 = schema["paths"]["/deployments"]["post"]["responses"]["503"]

    assert runs_get_404["content"]["application/json"]["example"] == {
        "error": {"code": "not_found", "message": "run not found: run_missing"}
    }
    assert stream_409["content"]["application/json"]["example"]["error"]["code"] == "event_replay_gap"
    assert approval_410["content"]["application/json"]["example"]["error"]["code"] == "approval_expired"
    assert deployment_503["content"]["application/json"]["example"]["error"]["code"] == (
        "deployment_unavailable"
    )


def test_apps_api_main_exposes_app_without_running_uvicorn(monkeypatch, tmp_path: Path):
    def _unexpected_run(*args, **kwargs):
        raise AssertionError("uvicorn.run should not be called during module import")

    monkeypatch.setenv("PYCLAW_API_WORKSPACE_ROOTS", str(tmp_path))
    monkeypatch.setenv("PYCLAW_API_DB_PATH", str(tmp_path / "runtime.sqlite3"))
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=_unexpected_run))
    sys.modules.pop("apps.api.main", None)

    module = importlib.import_module("apps.api.main")

    assert isinstance(module.app, FastAPI)


def test_require_supported_python_raises_clear_error_for_python_39():
    try:
        require_supported_python(component="Pyclaw API", version_info=(3, 9, 19))
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected unsupported Python version to raise RuntimeError")

    assert "Pyclaw API requires Python 3.10+" in message
    assert "current interpreter is Python 3.9.19" in message
