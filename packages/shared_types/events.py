from __future__ import annotations

from typing import Mapping

from .enums import EventType, RunStatus, TaskStatus
from .ids import ApprovalId, ArtifactId, EventId, RunId, TaskId
from .models import JSONValue, Run, RunEvent


def build_run_event(
    run_id: RunId,
    event_type: EventType,
    *,
    event_id: EventId | None = None,
    message: str | None = None,
    run_status: RunStatus | None = None,
    task_id: TaskId | None = None,
    task_status: TaskStatus | None = None,
    artifact_id: ArtifactId | None = None,
    approval_id: ApprovalId | None = None,
    payload: Mapping[str, JSONValue] | None = None,
) -> RunEvent:
    return RunEvent(
        run_id=run_id,
        event_id=event_id or EventId.generate(),
        event_type=event_type,
        message=message,
        run_status=run_status,
        task_id=task_id,
        task_status=task_status,
        artifact_id=artifact_id,
        approval_id=approval_id,
        payload=dict(payload or {}),
    )


def build_run_status_event(
    run: Run,
    event_type: EventType,
    *,
    message: str | None = None,
    payload: Mapping[str, JSONValue] | None = None,
) -> RunEvent:
    return build_run_event(
        run_id=run.run_id,
        event_type=event_type,
        message=message,
        run_status=run.status,
        payload=payload,
    )


__all__ = ["build_run_event", "build_run_status_event"]
