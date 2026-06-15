from __future__ import annotations

import json

from fastapi.testclient import TestClient

from apps.api import create_app
from packages.shared_types import EventType, RunEvent, RunResult, RunStatus, new_event_id, new_run_id


class _FakePlatformAPI:
    def __init__(self) -> None:
        run_id = str(new_run_id())
        self.run_id = run_id
        self.runs = {
            run_id: RunResult(run_id=run_id, status=RunStatus.RUNNING, summary=None),
        }

    async def get_run(self, run_id: str) -> RunResult | None:
        return self.runs.get(run_id)

    async def stream_run_events(self, run_id: str, *, last_event_id: str | None = None):
        yield RunEvent(
            run_id=run_id,
            event_id=new_event_id(),
            event_type=EventType.RUN_CREATED,
            sequence=1,
            run_status=RunStatus.QUEUED,
            message="Run created",
        )
        yield RunEvent(
            run_id=run_id,
            event_id=new_event_id(),
            event_type=EventType.RUN_STARTED,
            sequence=2,
            run_status=RunStatus.RUNNING,
            message="Run started",
        )


def test_run_event_stream_replays_fake_events():
    platform_api = _FakePlatformAPI()
    app = create_app(platform_api=platform_api)

    with TestClient(app) as client:
        with client.stream("GET", f"/runs/{platform_api.run_id}/events/stream") as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: run.created" in body
    assert "event: run.started" in body
    data_lines = [line.removeprefix("data: ") for line in body.splitlines() if line.startswith("data: ")]
    payloads = [json.loads(line) for line in data_lines]
    assert payloads[0]["sequence"] == 1
    assert payloads[1]["sequence"] == 2


def test_run_event_stream_returns_404_when_run_missing():
    app = create_app(platform_api=_FakePlatformAPI())

    with TestClient(app) as client:
        response = client.get("/runs/run_missing/events/stream")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "run not found: run_missing"}
    }
