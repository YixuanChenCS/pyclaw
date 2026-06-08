from __future__ import annotations

from typing import TypeVar
from uuid import uuid4

TEntityId = TypeVar("TEntityId", bound="EntityId")


class EntityId(str):
    """String-backed identifier with a stable entity prefix."""

    prefix = "id"

    def __new__(cls, value: str) -> "EntityId":
        if not value or not isinstance(value, str):
            raise ValueError(f"{cls.__name__} requires a non-empty string value")
        return str.__new__(cls, value)

    @classmethod
    def generate(cls: type[TEntityId]) -> TEntityId:
        return cls(f"{cls.prefix}_{uuid4().hex}")


class WorkspaceId(EntityId):
    prefix = "ws"


class SessionId(EntityId):
    prefix = "session"


class RunId(EntityId):
    prefix = "run"


class TaskId(EntityId):
    prefix = "task"


class ArtifactId(EntityId):
    prefix = "artifact"


class ApprovalId(EntityId):
    prefix = "approval"


class EventId(EntityId):
    prefix = "event"


def new_workspace_id() -> WorkspaceId:
    return WorkspaceId.generate()


def new_session_id() -> SessionId:
    return SessionId.generate()


def new_run_id() -> RunId:
    return RunId.generate()


def new_task_id() -> TaskId:
    return TaskId.generate()


def new_artifact_id() -> ArtifactId:
    return ArtifactId.generate()


def new_approval_id() -> ApprovalId:
    return ApprovalId.generate()


def new_event_id() -> EventId:
    return EventId.generate()


__all__ = [
    "ApprovalId",
    "ArtifactId",
    "EntityId",
    "EventId",
    "RunId",
    "SessionId",
    "TaskId",
    "WorkspaceId",
    "new_approval_id",
    "new_artifact_id",
    "new_event_id",
    "new_run_id",
    "new_session_id",
    "new_task_id",
    "new_workspace_id",
]
