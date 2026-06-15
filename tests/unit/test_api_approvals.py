from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api import create_app
from packages.shared_types import ApprovalRecord, EntityNotFoundError, InvalidRunStateError


class _FakePlatformAPI:
    def __init__(self) -> None:
        self.approvals = {
            "approval_pending": ApprovalRecord(
                approval_id="approval_pending",
                run_id="run_alpha",
                status="pending",
                kind="command",
                reason="Need approval to continue",
                command_argv=("git", "push"),
            ),
            "approval_approved": ApprovalRecord(
                approval_id="approval_approved",
                run_id="run_alpha",
                status="approved",
                kind="patch",
                reason="Apply risky patch",
                patch_id="artifact_patch",
                approved=True,
                comment="Looks good",
            ),
            "approval_rejected": ApprovalRecord(
                approval_id="approval_rejected",
                run_id="run_beta",
                status="rejected",
                kind="generic",
                reason="Need human review",
                approved=False,
                comment="Do not proceed",
            ),
        }
        self.list_calls: list[tuple[str | None, str | None]] = []
        self.decide_calls: list[tuple[str, bool, str | None]] = []
        self.not_found_ids: set[str] = set()
        self.conflict_ids: set[str] = set()

    async def list_approvals(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> tuple[ApprovalRecord, ...]:
        self.list_calls.append((run_id, status))
        approvals = tuple(self.approvals.values())
        if run_id is not None:
            approvals = tuple(approval for approval in approvals if str(approval.run_id) == run_id)
        if status is not None:
            approvals = tuple(approval for approval in approvals if approval.status == status)
        return approvals

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        if approval_id in self.not_found_ids:
            return None
        return self.approvals.get(approval_id)

    async def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        comment: str | None = None,
    ) -> ApprovalRecord:
        if approval_id in self.not_found_ids:
            raise EntityNotFoundError("approval", approval_id)
        if approval_id in self.conflict_ids:
            raise InvalidRunStateError(f"Approval {approval_id} is already finalized")
        self.decide_calls.append((approval_id, approved, comment))
        current = self.approvals[approval_id]
        updated = ApprovalRecord(
            approval_id=current.approval_id,
            run_id=current.run_id,
            status="approved" if approved else "rejected",
            kind=current.kind,
            reason=current.reason,
            task_id=current.task_id,
            patch_id=current.patch_id,
            command_argv=current.command_argv,
            approved=approved,
            created_at=current.created_at,
            updated_at=current.updated_at,
            decided_at=current.decided_at,
            expires_at=current.expires_at,
            reviewer=current.reviewer,
            comment=comment,
        )
        self.approvals[approval_id] = updated
        return updated


def test_list_approvals_returns_serialized_records():
    platform_api = _FakePlatformAPI()
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get("/approvals")

    assert response.status_code == 200
    assert len(response.json()) == 3
    assert response.json()[0]["approval_id"] == "approval_pending"
    assert response.json()[0]["kind"] == "command"


def test_list_approvals_passes_run_id_filter():
    platform_api = _FakePlatformAPI()
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get("/approvals", params={"run_id": "run_beta"})

    assert response.status_code == 200
    assert [item["approval_id"] for item in response.json()] == ["approval_rejected"]
    assert platform_api.list_calls[-1] == ("run_beta", None)


def test_list_approvals_passes_status_filter():
    platform_api = _FakePlatformAPI()
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get("/approvals", params={"status": "approved"})

    assert response.status_code == 200
    assert [item["approval_id"] for item in response.json()] == ["approval_approved"]
    assert platform_api.list_calls[-1] == (None, "approved")


def test_get_approval_returns_details():
    platform_api = _FakePlatformAPI()
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get("/approvals/approval_pending")

    assert response.status_code == 200
    assert response.json()["approval_id"] == "approval_pending"
    assert response.json()["run_id"] == "run_alpha"
    assert response.json()["status"] == "pending"
    assert response.json()["command_argv"] == ["git", "push"]


def test_get_approval_returns_404_when_missing():
    platform_api = _FakePlatformAPI()
    platform_api.not_found_ids.add("approval_missing")
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get("/approvals/approval_missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "approval not found: approval_missing"}
    }


def test_decide_approval_accepts_approved_status():
    platform_api = _FakePlatformAPI()
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.post(
            "/approvals/approval_pending/decision",
            json={"decision": "approved", "comment": "Looks good"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "approved"
    assert response.json()["comment"] == "Looks good"
    assert platform_api.decide_calls == [("approval_pending", True, "Looks good")]


def test_decide_approval_accepts_rejected_status():
    platform_api = _FakePlatformAPI()
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.post(
            "/approvals/approval_pending/decision",
            json={"decision": "rejected", "comment": "Please do not modify this file."},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "rejected"
    assert platform_api.decide_calls == [
        ("approval_pending", False, "Please do not modify this file.")
    ]


def test_decide_approval_returns_422_for_invalid_decision():
    app = create_app(platform_api=_FakePlatformAPI())

    with TestClient(app) as client:
        response = client.post(
            "/approvals/approval_pending/decision",
            json={"decision": "maybe", "comment": "Unsure"},
        )

    assert response.status_code == 422


def test_decide_approval_returns_404_when_missing():
    platform_api = _FakePlatformAPI()
    platform_api.not_found_ids.add("approval_missing")
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.post(
            "/approvals/approval_missing/decision",
            json={"decision": "approved"},
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "approval not found: approval_missing"}
    }


def test_decide_approval_returns_409_when_already_finalized():
    platform_api = _FakePlatformAPI()
    platform_api.conflict_ids.add("approval_approved")
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.post(
            "/approvals/approval_approved/decision",
            json={"decision": "approved"},
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "invalid_state_transition",
            "message": "Approval approval_approved is already finalized",
        }
    }
