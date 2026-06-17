from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api import create_app
from packages.shared_types import (
    EntityNotFoundError,
    EventType,
    InvalidRunStateError,
    RecoveryOption,
    RecoveryState,
    RecoveryStatus,
    RunEvent,
    RunRequest,
    RunResult,
    RunStatus,
    new_event_id,
    new_run_id,
)


class _FakePlatformAPI:
    def __init__(self) -> None:
        self.create_run_request: RunRequest | None = None
        self.run_id_to_create = str(new_run_id())
        self.runs: dict[str, RunResult] = {}
        self.events: dict[str, tuple[RunEvent, ...]] = {}
        self.cancelled_run_ids: list[str] = []
        self.missing_event_run_ids: set[str] = set()
        self.missing_cancel_run_ids: set[str] = set()
        self.conflict_cancel_run_ids: set[str] = set()
        self.recovery_by_run_id: dict[str, RecoveryStatus] = {}
        self.rollback_calls: list[tuple[str, str]] = []
        self.list_runs_calls: list[tuple[str | None, str | None, str | None, int | None]] = []

    async def create_run(self, request: RunRequest) -> str:
        self.create_run_request = request
        return self.run_id_to_create

    async def list_runs(
        self,
        workspace_id: str | None = None,
        *,
        session_id: str | None = None,
        status: str | RunStatus | None = None,
        limit: int | None = None,
    ) -> tuple[RunResult, ...]:
        self.list_runs_calls.append(
            (
                workspace_id,
                session_id,
                None if status is None else str(status),
                limit,
            )
        )
        runs = tuple(self.runs.values())
        if status is not None:
            status_value = status.value if isinstance(status, RunStatus) else status
            runs = tuple(run for run in runs if run.status.value == status_value)
        if limit is not None:
            runs = runs[:limit]
        return runs

    async def get_run(self, run_id: str) -> RunResult | None:
        return self.runs.get(run_id)

    async def get_run_summary(self, run_id: str) -> RunResult | None:
        return self.runs.get(run_id)

    async def list_run_events(self, run_id: str) -> tuple[RunEvent, ...]:
        if run_id in self.missing_event_run_ids:
            raise EntityNotFoundError("run", run_id)
        return self.events.get(run_id, ())

    async def cancel_run(self, run_id: str) -> None:
        if run_id in self.missing_cancel_run_ids:
            raise EntityNotFoundError("run", run_id)
        if run_id in self.conflict_cancel_run_ids:
            raise InvalidRunStateError(f"Cannot cancel terminal run {run_id}")
        self.cancelled_run_ids.append(run_id)

    async def get_recovery_status(self, run_id: str) -> RecoveryStatus | None:
        if run_id not in self.runs:
            raise EntityNotFoundError("run", run_id)
        return self.recovery_by_run_id.get(run_id)

    async def rollback_recovery(self, run_id: str, task_id: str) -> RecoveryStatus:
        if run_id not in self.runs:
            raise EntityNotFoundError("run", run_id)
        self.rollback_calls.append((run_id, task_id))
        if run_id not in self.recovery_by_run_id:
            raise InvalidRunStateError(f"No rollback snapshot exists for task {task_id}")
        recovery = RecoveryStatus(
            run_id=run_id,
            recovery_state=RecoveryState.ROLLBACK_REQUIRED_REVIEW,
            reason="Rollback completed; manual review is required before continuing the run.",
            task_id=task_id,
            recovery_options=(RecoveryOption.REVIEW_MANUALLY, RecoveryOption.ABORT),
            rollback_task_id=task_id,
        )
        self.recovery_by_run_id[run_id] = recovery
        return recovery


def test_create_run_returns_202_and_converts_request_body():
    platform_api = _FakePlatformAPI()
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={
                "workspace_id": "ws_test",
                "session_id": "session_test",
                "prompt": "Fix the bug",
                "run_id": "run_custom",
                "target_paths": ["services/agent_core/runner.py"],
            },
        )

    assert response.status_code == 202
    assert response.json() == {
        "run_id": platform_api.run_id_to_create,
        "status": "queued",
    }
    assert platform_api.create_run_request is not None
    assert str(platform_api.create_run_request.workspace_id) == "ws_test"
    assert str(platform_api.create_run_request.session_id) == "session_test"
    assert str(platform_api.create_run_request.run_id) == "run_custom"
    assert platform_api.create_run_request.prompt == "Fix the bug"
    assert platform_api.create_run_request.target_paths == ("services/agent_core/runner.py",)


def test_create_run_returns_422_for_invalid_request_body():
    app = create_app(platform_api=_FakePlatformAPI())

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={
                "workspace_id": "ws_test",
                "session_id": "session_test",
                "prompt": "",
            },
        )

    assert response.status_code == 422


def test_list_runs_returns_200():
    platform_api = _FakePlatformAPI()
    first_run_id = str(new_run_id())
    second_run_id = str(new_run_id())
    platform_api.runs[first_run_id] = RunResult(run_id=first_run_id, status=RunStatus.QUEUED, summary="Queued")
    platform_api.runs[second_run_id] = RunResult(run_id=second_run_id, status=RunStatus.SUCCEEDED, summary="Done")
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get("/runs")

    assert response.status_code == 200
    assert [item["run_id"] for item in response.json()] == [first_run_id, second_run_id]


def test_list_runs_passes_filters():
    platform_api = _FakePlatformAPI()
    queued_run_id = str(new_run_id())
    done_run_id = str(new_run_id())
    platform_api.runs[queued_run_id] = RunResult(run_id=queued_run_id, status=RunStatus.QUEUED, summary="Queued")
    platform_api.runs[done_run_id] = RunResult(run_id=done_run_id, status=RunStatus.SUCCEEDED, summary="Done")
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get(
            "/runs",
            params={
                "workspace_id": "ws_test",
                "session_id": "session_test",
                "status": "succeeded",
                "limit": 1,
            },
        )

    assert response.status_code == 200
    assert [item["run_id"] for item in response.json()] == [done_run_id]
    assert platform_api.list_runs_calls[-1] == ("ws_test", "session_test", "succeeded", 1)


def test_get_run_returns_200_when_run_exists():
    platform_api = _FakePlatformAPI()
    run_id = str(new_run_id())
    platform_api.runs[run_id] = RunResult(
        run_id=run_id,
        status=RunStatus.QUEUED,
        summary="Queued",
    )
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    assert response.json()["run_id"] == run_id
    assert response.json()["status"] == "queued"


def test_get_run_returns_404_when_run_is_missing():
    app = create_app(platform_api=_FakePlatformAPI())

    with TestClient(app) as client:
        response = client.get("/runs/run_missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "run not found: run_missing"}
    }


def test_get_run_recovery_returns_current_recovery_status():
    platform_api = _FakePlatformAPI()
    run_id = str(new_run_id())
    platform_api.runs[run_id] = RunResult(run_id=run_id, status=RunStatus.NEEDS_RECOVERY, summary=None)
    platform_api.recovery_by_run_id[run_id] = RecoveryStatus(
        run_id=run_id,
        recovery_state=RecoveryState.ROLLBACK_AVAILABLE,
        reason="Patch task interrupted.",
        task_id="task_patch",
        recovery_options=(RecoveryOption.ROLLBACK_IF_AVAILABLE, RecoveryOption.REVIEW_MANUALLY),
        rollback_task_id="task_patch",
    )
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get(f"/runs/{run_id}/recovery")

    assert response.status_code == 200
    assert response.json()["run_id"] == run_id
    assert response.json()["recovery_state"] == "rollback_available"
    assert response.json()["rollback_task_id"] == "task_patch"


def test_get_run_recovery_returns_409_when_run_is_not_in_recovery():
    platform_api = _FakePlatformAPI()
    run_id = str(new_run_id())
    platform_api.runs[run_id] = RunResult(run_id=run_id, status=RunStatus.RUNNING, summary=None)
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get(f"/runs/{run_id}/recovery")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "invalid_state_transition",
            "message": f"Run {run_id} is not waiting on recovery.",
        }
    }


def test_rollback_run_recovery_returns_updated_recovery_status():
    platform_api = _FakePlatformAPI()
    run_id = str(new_run_id())
    platform_api.runs[run_id] = RunResult(run_id=run_id, status=RunStatus.NEEDS_RECOVERY, summary=None)
    platform_api.recovery_by_run_id[run_id] = RecoveryStatus(
        run_id=run_id,
        recovery_state=RecoveryState.ROLLBACK_AVAILABLE,
        reason="Patch task interrupted.",
        task_id="task_patch",
        recovery_options=(RecoveryOption.ROLLBACK_IF_AVAILABLE, RecoveryOption.REVIEW_MANUALLY),
        rollback_task_id="task_patch",
    )
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.post(f"/runs/{run_id}/recovery/rollback", params={"task_id": "task_patch"})

    assert response.status_code == 200
    assert platform_api.rollback_calls == [(run_id, "task_patch")]
    assert response.json()["recovery_state"] == "rollback_required_review"


def test_rollback_run_recovery_returns_409_when_snapshot_is_unavailable():
    platform_api = _FakePlatformAPI()
    run_id = str(new_run_id())
    platform_api.runs[run_id] = RunResult(run_id=run_id, status=RunStatus.NEEDS_RECOVERY, summary=None)
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.post(f"/runs/{run_id}/recovery/rollback", params={"task_id": "task_patch"})

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "invalid_state_transition",
            "message": "No rollback snapshot exists for task task_patch",
        }
    }


def test_list_run_events_returns_serialized_events():
    platform_api = _FakePlatformAPI()
    run_id = str(new_run_id())
    platform_api.events[run_id] = (
        RunEvent(
            run_id=run_id,
            event_id=new_event_id(),
            event_type=EventType.RUN_CREATED,
            sequence=1,
            run_status=RunStatus.QUEUED,
            message="Run created",
        ),
    )
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get(f"/runs/{run_id}/events")

    assert response.status_code == 200
    assert response.json()[0]["run_id"] == run_id
    assert response.json()[0]["event_type"] == "run.created"
    assert response.json()[0]["sequence"] == 1


def test_list_run_events_returns_404_when_run_is_missing():
    platform_api = _FakePlatformAPI()
    platform_api.missing_event_run_ids.add("run_missing")
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get("/runs/run_missing/events")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "run not found: run_missing"}
    }


def test_run_summary_returns_summary_for_completed_run():
    platform_api = _FakePlatformAPI()
    run_id = str(new_run_id())
    platform_api.runs[run_id] = RunResult(run_id=run_id, status=RunStatus.SUCCEEDED, summary="All done")
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get(f"/runs/{run_id}/summary")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": run_id,
        "status": "succeeded",
        "summary": "All done",
        "completed": True,
    }


def test_run_summary_returns_null_summary_for_incomplete_run():
    platform_api = _FakePlatformAPI()
    run_id = str(new_run_id())
    platform_api.runs[run_id] = RunResult(run_id=run_id, status=RunStatus.RUNNING, summary="Still going")
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.get(f"/runs/{run_id}/summary")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": run_id,
        "status": "running",
        "summary": None,
        "completed": False,
    }


def test_run_summary_returns_404_when_run_missing():
    app = create_app(platform_api=_FakePlatformAPI())

    with TestClient(app) as client:
        response = client.get("/runs/run_missing/summary")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "run not found: run_missing"}
    }


def test_cancel_run_returns_202_when_cancellation_is_accepted():
    platform_api = _FakePlatformAPI()
    run_id = str(new_run_id())
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.post(f"/runs/{run_id}/cancel")

    assert response.status_code == 202
    assert response.json() == {"run_id": run_id, "status": "cancelling"}
    assert platform_api.cancelled_run_ids == [run_id]


def test_cancel_run_returns_404_when_run_is_missing():
    platform_api = _FakePlatformAPI()
    platform_api.missing_cancel_run_ids.add("run_missing")
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.post("/runs/run_missing/cancel")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "run not found: run_missing"}
    }


def test_cancel_run_returns_409_for_terminal_run():
    platform_api = _FakePlatformAPI()
    run_id = "run_terminal"
    platform_api.conflict_cancel_run_ids.add(run_id)
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        response = client.post(f"/runs/{run_id}/cancel")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "invalid_state_transition",
            "message": f"Cannot cancel terminal run {run_id}",
        }
    }
