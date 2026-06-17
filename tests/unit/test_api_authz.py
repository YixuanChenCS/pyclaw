from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api import APIAuthPrincipal, APIConfig, create_app, load_api_config_from_env
from packages.shared_types import ApprovalRecord, HealthCheckResult, RunRequest, RunResult, RunStatus, new_run_id


class _AuthzPlatformAPI:
    def __init__(self) -> None:
        self.run_id = str(new_run_id())
        self.create_run_request: RunRequest | None = None
        self.runs = {
            self.run_id: RunResult(run_id=self.run_id, status=RunStatus.QUEUED, summary="Queued"),
        }
        self.approvals = {
            "approval_pending": ApprovalRecord(
                approval_id="approval_pending",
                run_id=self.run_id,
                status="pending",
                kind="command",
                reason="Needs approval",
                command_argv=("git", "push"),
            )
        }

    async def get_health(self) -> HealthCheckResult:
        return HealthCheckResult(service="platform-api", status="ready")

    async def list_runs(self, workspace_id: str | None = None, *, session_id=None, status=None, limit=None):
        return tuple(self.runs.values())

    async def create_run(self, request: RunRequest) -> str:
        self.create_run_request = request
        return self.run_id

    async def list_approvals(self, *, run_id: str | None = None, status: str | None = None):
        return tuple(self.approvals.values())

    async def decide_approval(self, approval_id: str, *, approved: bool, comment: str | None = None):
        current = self.approvals[approval_id]
        updated = ApprovalRecord(
            approval_id=current.approval_id,
            run_id=current.run_id,
            status="approved" if approved else "rejected",
            kind=current.kind,
            reason=current.reason,
            command_argv=current.command_argv,
            approved=approved,
            comment=comment,
        )
        self.approvals[approval_id] = updated
        return updated


def test_load_api_config_from_env_parses_auth_principals_json():
    config = load_api_config_from_env(
        {
            "PYCLAW_API_AUTH_PRINCIPALS": """
            [
              {"token":"reader-token","subject":"reader","permissions":["control_plane:read"]},
              {"token":"deployer-token","subject":"deployer","permissions":["deployments:create"]}
            ]
            """,
        }
    )

    assert config.auth_principals == (
        APIAuthPrincipal(token="reader-token", subject="reader", permissions=("control_plane:read",)),
        APIAuthPrincipal(token="deployer-token", subject="deployer", permissions=("deployments:create",)),
    )


def test_load_api_config_from_env_rejects_invalid_auth_principals():
    try:
        load_api_config_from_env({"PYCLAW_API_AUTH_PRINCIPALS": "{not-json}"})
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected invalid auth principal JSON to raise ValueError")

    assert "PYCLAW_API_AUTH_PRINCIPALS" in message


def test_control_plane_read_principal_can_read_but_cannot_write():
    app = create_app(
        platform_api=_AuthzPlatformAPI(),
        config=APIConfig(
            auth_principals=(
                APIAuthPrincipal(
                    token="reader-token",
                    subject="reader",
                    permissions=("control_plane:read",),
                ),
            )
        ),
    )
    headers = {"Authorization": "Bearer reader-token"}

    with TestClient(app) as client:
        list_response = client.get("/runs", headers=headers)
        create_response = client.post(
            "/runs",
            headers=headers,
            json={"workspace_id": "ws_test", "session_id": "session_test", "prompt": "Run agent"},
        )

    assert list_response.status_code == 200
    assert create_response.status_code == 403
    assert create_response.json() == {
        "error": {
            "code": "forbidden",
            "message": "Permission denied. Missing required permission: runs:create",
        }
    }


def test_control_plane_write_principal_can_read_and_write():
    platform_api = _AuthzPlatformAPI()
    app = create_app(
        platform_api=platform_api,
        config=APIConfig(
            auth_principals=(
                APIAuthPrincipal(
                    token="writer-token",
                    subject="writer",
                    permissions=("control_plane:write",),
                ),
            )
        ),
    )
    headers = {"Authorization": "Bearer writer-token"}

    with TestClient(app) as client:
        list_response = client.get("/runs", headers=headers)
        create_response = client.post(
            "/runs",
            headers=headers,
            json={"workspace_id": "ws_test", "session_id": "session_test", "prompt": "Run agent"},
        )

    assert list_response.status_code == 200
    assert create_response.status_code == 202
    assert platform_api.create_run_request is not None


def test_approvals_read_principal_cannot_decide_approval():
    app = create_app(
        platform_api=_AuthzPlatformAPI(),
        config=APIConfig(
            auth_principals=(
                APIAuthPrincipal(
                    token="reviewer-token",
                    subject="reviewer",
                    permissions=("approvals:read",),
                ),
            )
        ),
    )
    headers = {"Authorization": "Bearer reviewer-token"}

    with TestClient(app) as client:
        list_response = client.get("/approvals", headers=headers)
        decide_response = client.post(
            "/approvals/approval_pending/decision",
            headers=headers,
            json={"decision": "approved"},
        )

    assert list_response.status_code == 200
    assert decide_response.status_code == 403
    assert decide_response.json()["error"]["code"] == "forbidden"
