from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import APIConfig, create_app
from apps.api.app import ArtifactDetailResponse
from packages.shared_types import ApprovalRecord, EventType, RunEvent, RunResult, RunStatus, new_approval_id, new_event_id, new_run_id


class _CreateRunPlatformAPI:
    def __init__(self) -> None:
        self.run_id = str(new_run_id())
        self.workspace_paths: list[str] = []
        self.run_requests: list[object] = []

    async def create_run(self, request) -> str:
        self.run_requests.append(request)
        return self.run_id

    async def create_run_from_workspace(
        self,
        *,
        workspace_path: str,
        prompt: str,
        target_paths=(),
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        self.workspace_paths.append(workspace_path)
        return run_id or self.run_id

    async def get_health(self):
        from packages.shared_types import HealthCheckResult

        return HealthCheckResult(service="platform-api", status="ready")


class _WorkflowPlatformAPI:
    def __init__(self) -> None:
        self.run_id = str(new_run_id())
        self.approval_id = str(new_approval_id())
        self.artifact_id = "artifact_summary"
        self.workspace_paths: list[str] = []
        self.run = RunResult(run_id=self.run_id, status=RunStatus.WAITING_FOR_APPROVAL, summary=None)
        self.approval = ApprovalRecord(
            approval_id=self.approval_id,
            run_id=self.run_id,
            status="pending",
            kind="command",
            reason="Need approval",
            command_argv=("git", "push"),
        )
        self.artifact = ArtifactDetailResponse(
            artifact_id=self.artifact_id,
            run_id=self.run_id,
            artifact_type="summary",
            label="summary.txt",
            uri="memory://summary",
            created_at="2026-06-15T00:00:00Z",
            content="Run completed",
            content_inline=True,
            content_kind="text",
        )
        self.events = [
            RunEvent(
                run_id=self.run_id,
                event_id=new_event_id(),
                event_type=EventType.RUN_CREATED,
                sequence=1,
                run_status=RunStatus.QUEUED,
                message="Run created",
            ),
            RunEvent(
                run_id=self.run_id,
                event_id=new_event_id(),
                event_type=EventType.RUN_QUEUED,
                sequence=2,
                run_status=RunStatus.QUEUED,
                message="Run queued",
            ),
            RunEvent(
                run_id=self.run_id,
                event_id=new_event_id(),
                event_type=EventType.APPROVAL_REQUESTED,
                sequence=3,
                run_status=RunStatus.WAITING_FOR_APPROVAL,
                approval_id=self.approval_id,
                message="Approval requested",
            ),
        ]

    async def create_run_from_workspace(
        self,
        *,
        workspace_path: str,
        prompt: str,
        target_paths=(),
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        self.workspace_paths.append(workspace_path)
        return self.run_id

    async def get_run(self, run_id: str) -> RunResult | None:
        return self.run if run_id == self.run_id else None

    async def get_run_summary(self, run_id: str) -> RunResult | None:
        return self.run if run_id == self.run_id else None

    async def list_run_events(self, run_id: str):
        return tuple(self.events) if run_id == self.run_id else ()

    async def stream_run_events(self, run_id: str, *, last_event_id: str | None = None):
        for event in self.events:
            yield event

    async def list_approvals(self, *, run_id: str | None = None, status: str | None = None):
        approvals = (self.approval,)
        if run_id is not None:
            approvals = tuple(item for item in approvals if str(item.run_id) == run_id)
        if status is not None:
            approvals = tuple(item for item in approvals if item.status == status)
        return approvals

    async def get_approval(self, approval_id: str):
        return self.approval if approval_id == self.approval_id else None

    async def decide_approval(self, approval_id: str, *, approved: bool, comment: str | None = None):
        self.approval = ApprovalRecord(
            approval_id=self.approval.approval_id,
            run_id=self.approval.run_id,
            status="approved" if approved else "rejected",
            kind=self.approval.kind,
            reason=self.approval.reason,
            command_argv=self.approval.command_argv,
            approved=approved,
            comment=comment,
        )
        self.run = RunResult(run_id=self.run_id, status=RunStatus.SUCCEEDED, summary="Run completed")
        self.events.extend(
            [
                RunEvent(
                    run_id=self.run_id,
                    event_id=new_event_id(),
                    event_type=EventType.APPROVAL_RESOLVED,
                    sequence=4,
                    run_status=RunStatus.WAITING_FOR_APPROVAL,
                    approval_id=self.approval_id,
                    message="Approval granted",
                ),
                RunEvent(
                    run_id=self.run_id,
                    event_id=new_event_id(),
                    event_type=EventType.ARTIFACT_CREATED,
                    sequence=5,
                    run_status=RunStatus.SUCCEEDED,
                    message="Artifact created",
                ),
            ]
        )
        return self.approval

    async def list_artifacts(self, run_id: str):
        return (self.artifact,) if run_id == self.run_id else ()

    async def get_artifact(self, artifact_id: str):
        return self.artifact if artifact_id == self.artifact_id else None

    async def get_health(self):
        from packages.shared_types import HealthCheckResult

        return HealthCheckResult(service="platform-api", status="ready")


def test_workspace_allowed_success(tmp_path: Path):
    workspace = tmp_path / "allowed" / "repo"
    workspace.mkdir(parents=True)
    platform_api = _CreateRunPlatformAPI()
    app = create_app(
        platform_api=platform_api,
        config=APIConfig(allowed_workspace_roots=(str(tmp_path / "allowed"),)),
    )

    with TestClient(app) as client:
        response = client.post("/runs", json={"workspace_path": str(workspace), "prompt": "Run agent"})

    assert response.status_code == 202
    assert platform_api.workspace_paths == [str(workspace.resolve())]


def test_workspace_outside_allowlist_rejected(tmp_path: Path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    app = create_app(
        platform_api=_CreateRunPlatformAPI(),
        config=APIConfig(allowed_workspace_roots=(str(allowed_root),)),
    )

    with TestClient(app) as client:
        response = client.post("/runs", json={"workspace_path": str(outside), "prompt": "Run agent"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "workspace_not_allowed"


def test_workspace_path_traversal_rejected(tmp_path: Path):
    parent = tmp_path / "parent"
    allowed_root = parent / "allowed"
    outside = parent / "outside"
    allowed_root.mkdir(parents=True)
    outside.mkdir()
    app = create_app(
        platform_api=_CreateRunPlatformAPI(),
        config=APIConfig(allowed_workspace_roots=(str(allowed_root),)),
    )
    traversal_path = allowed_root / ".." / "outside"

    with TestClient(app) as client:
        response = client.post("/runs", json={"workspace_path": str(traversal_path), "prompt": "Run agent"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "workspace_not_allowed"


def test_invalid_workspace_rejected(tmp_path: Path):
    missing = tmp_path / "missing"
    app = create_app(platform_api=_CreateRunPlatformAPI(), config=APIConfig())

    with TestClient(app) as client:
        response = client.post("/runs", json={"workspace_path": str(missing), "prompt": "Run agent"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_workspace"


def test_api_token_disabled_preserves_behavior():
    platform_api = _CreateRunPlatformAPI()
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={"workspace_id": "ws_test", "session_id": "session_test", "prompt": "Run agent"},
        )

    assert response.status_code == 202


def test_api_token_enabled_accepts_valid_bearer_token():
    platform_api = _CreateRunPlatformAPI()
    app = create_app(platform_api=platform_api, config=APIConfig(api_token="secret-token"))

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={"workspace_id": "ws_test", "session_id": "session_test", "prompt": "Run agent"},
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 202


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_api_token_enabled_rejects_missing_or_invalid_token(authorization: str | None):
    platform_api = _CreateRunPlatformAPI()
    app = create_app(platform_api=platform_api, config=APIConfig(api_token="secret-token"))

    headers = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={"workspace_id": "ws_test", "session_id": "session_test", "prompt": "Run agent"},
            headers=headers,
        )

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Missing or invalid bearer token."}
    }


def test_health_remains_accessible_without_token():
    platform_api = _CreateRunPlatformAPI()
    app = create_app(platform_api=platform_api, config=APIConfig(api_token="secret-token"))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_cors_allows_configured_origin():
    app = create_app(
        platform_api=_CreateRunPlatformAPI(),
        config=APIConfig(cors_allowed_origins=("http://localhost:3000",), api_token="secret-token"),
    )

    with TestClient(app) as client:
        response = client.options(
            "/runs",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.headers["access-control-allow-origin"] != "*"


def test_artifact_api_does_not_read_arbitrary_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    arbitrary_file = tmp_path / "secret.txt"
    arbitrary_file.write_text("should-not-be-read", encoding="utf-8")
    app = create_app(platform_api=_WorkflowPlatformAPI())

    def fail_read_text(*args, **kwargs):
        raise AssertionError("artifact route must not read filesystem paths directly")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    with TestClient(app) as client:
        response = client.get("/artifacts/artifact_summary")

    assert response.status_code == 200
    assert response.json()["content"] == "Run completed"


def test_end_to_end_api_workflow(tmp_path: Path):
    workspace = tmp_path / "allowed" / "repo"
    workspace.mkdir(parents=True)
    platform_api = _WorkflowPlatformAPI()
    app = create_app(
        platform_api=platform_api,
        config=APIConfig(
            allowed_workspace_roots=(str(tmp_path / "allowed"),),
            api_token="secret-token",
        ),
    )

    headers = {"Authorization": "Bearer secret-token"}
    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"workspace_path": str(workspace), "prompt": "Run workflow"},
            headers=headers,
        )
        run_id = create_response.json()["run_id"]
        run_response = client.get(f"/runs/{run_id}", headers=headers)
        events_response = client.get(f"/runs/{run_id}/events", headers=headers)
        with client.stream("GET", f"/runs/{run_id}/events/stream", headers=headers) as stream_response:
            stream_body = "".join(stream_response.iter_text())
        approvals_response = client.get("/approvals", params={"run_id": run_id}, headers=headers)
        approval_id = approvals_response.json()[0]["approval_id"]
        approval_response = client.get(f"/approvals/{approval_id}", headers=headers)
        decision_response = client.post(
            f"/approvals/{approval_id}/decision",
            json={"decision": "approved", "comment": "Looks safe"},
            headers=headers,
        )
        summary_response = client.get(f"/runs/{run_id}/summary", headers=headers)
        artifacts_response = client.get(f"/runs/{run_id}/artifacts", headers=headers)
        artifact_id = artifacts_response.json()[0]["artifact_id"]
        artifact_response = client.get(f"/artifacts/{artifact_id}", headers=headers)

    assert create_response.status_code == 202
    assert run_response.json()["status"] == "waiting_for_approval"
    assert len(events_response.json()) >= 3
    assert "event: approval.requested" in stream_body
    assert approval_response.json()["status"] == "pending"
    assert decision_response.status_code == 202
    assert summary_response.json() == {
        "run_id": run_id,
        "status": "succeeded",
        "summary": "Run completed",
        "completed": True,
    }
    assert artifacts_response.json()[0]["artifact_id"] == artifact_id
    assert artifact_response.json()["content_inline"] is True
