from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from typing import Any, Mapping, Sequence, Union

from .enums import ArtifactType, EventType, RunStatus, TaskStatus
from .errors import ContractSerializationError
from .ids import (
    ApprovalId,
    ArtifactId,
    EventId,
    RunId,
    SessionId,
    TaskId,
    WorkspaceId,
    new_approval_id,
    new_artifact_id,
    new_event_id,
    new_run_id,
    new_session_id,
    new_task_id,
    new_workspace_id,
)

JSONValue = Union[
    str,
    int,
    float,
    bool,
    None,
    list["JSONValue"],
    dict[str, "JSONValue"],
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _to_json_value(value: Any) -> JSONValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_to_json_value(item) for item in value]
    if is_dataclass(value):
        return {item.name: _to_json_value(getattr(value, item.name)) for item in fields(value)}
    raise TypeError(f"Unsupported value for JSON serialization: {type(value)!r}")


@dataclass(frozen=True, slots=True)
class SerializableModel:
    def to_dict(self) -> dict[str, JSONValue]:
        try:
            return {
                item.name: _to_json_value(getattr(self, item.name))
                for item in fields(self)
            }
        except TypeError as exc:
            raise ContractSerializationError(str(exc)) from exc

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class Workspace(SerializableModel):
    root_path: str
    workspace_id: WorkspaceId = field(default_factory=new_workspace_id)
    branch: str | None = None
    commit_sha: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True, kw_only=True)
class Session(SerializableModel):
    workspace_id: WorkspaceId
    session_id: SessionId = field(default_factory=new_session_id)
    title: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True, kw_only=True)
class Run(SerializableModel):
    workspace_id: WorkspaceId
    session_id: SessionId
    prompt: str
    run_id: RunId = field(default_factory=new_run_id)
    status: RunStatus = RunStatus.QUEUED
    worker_id: str | None = None
    attempt: int = 0
    lease_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Task(SerializableModel):
    run_id: RunId
    title: str
    task_id: TaskId = field(default_factory=new_task_id)
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Artifact(SerializableModel):
    run_id: RunId
    artifact_type: ArtifactType
    artifact_id: ArtifactId = field(default_factory=new_artifact_id)
    task_id: TaskId | None = None
    label: str | None = None
    uri: str | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunRequest(SerializableModel):
    workspace_id: WorkspaceId
    session_id: SessionId
    prompt: str
    run_id: RunId | None = None
    target_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class RunResult(SerializableModel):
    run_id: RunId
    status: RunStatus
    summary: str | None = None
    artifacts: tuple[Artifact, ...] = ()
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FileSummary(SerializableModel):
    path: str
    summary: str | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolMatch(SerializableModel):
    name: str
    kind: str
    path: str
    line: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ImpactAnalysis(SerializableModel):
    changed_paths: tuple[str, ...] = ()
    impacted_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class RepoContextRequest(SerializableModel):
    workspace_id: WorkspaceId
    run_id: RunId
    prompt: str | None = None
    task_id: TaskId | None = None
    target_paths: tuple[str, ...] = ()
    max_files: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RepoContextResult(SerializableModel):
    workspace_id: WorkspaceId
    run_id: RunId
    file_summaries: tuple[FileSummary, ...] = ()
    repo_map: str | None = None
    symbols: tuple[SymbolMatch, ...] = ()
    dependency_hints: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True, kw_only=True)
class PatchProposal(SerializableModel):
    run_id: RunId
    artifact_id: ArtifactId = field(default_factory=new_artifact_id)
    task_id: TaskId | None = None
    summary: str | None = None
    unified_diff: str = ""
    target_paths: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandRequest(SerializableModel):
    run_id: RunId
    task_id: TaskId
    argv: tuple[str, ...]
    cwd: str | None = None
    timeout_seconds: int | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandResult(SerializableModel):
    run_id: RunId
    task_id: TaskId
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRequest(SerializableModel):
    run_id: RunId
    reason: str
    approval_id: ApprovalId = field(default_factory=new_approval_id)
    task_id: TaskId | None = None
    patch_id: ArtifactId | None = None
    command_argv: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalDecision(SerializableModel):
    approval_id: ApprovalId
    run_id: RunId
    approved: bool
    decided_at: datetime = field(default_factory=utc_now)
    reviewer: str | None = None
    comment: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RunEvent(SerializableModel):
    run_id: RunId
    event_type: EventType
    event_id: EventId = field(default_factory=new_event_id)
    sequence: int = 0
    message: str | None = None
    run_status: RunStatus | None = None
    task_id: TaskId | None = None
    task_status: TaskStatus | None = None
    artifact_id: ArtifactId | None = None
    approval_id: ApprovalId | None = None
    payload: Mapping[str, JSONValue] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentPlanStep(SerializableModel):
    task_id: TaskId
    title: str
    status: TaskStatus = TaskStatus.PENDING
    notes: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentPlan(SerializableModel):
    run_id: RunId
    steps: tuple[AgentPlanStep, ...] = ()
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentAction(SerializableModel):
    run_id: RunId
    task_id: TaskId | None = None
    message: str | None = None
    command_request: CommandRequest | None = None
    patch_proposal: PatchProposal | None = None
    approval_request: ApprovalRequest | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True, kw_only=True)
class WatchSubscription(SerializableModel):
    workspace_id: WorkspaceId
    subscription_id: str
    watched_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMMessage(SerializableModel):
    role: str
    content: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenUsage(SerializableModel):
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMResponse(SerializableModel):
    provider: str
    model: str
    content: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentRequest(SerializableModel):
    run_id: RunId
    workspace_id: WorkspaceId
    target: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentResult(SerializableModel):
    run_id: RunId
    status: str
    url: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthCheckResult(SerializableModel):
    service: str
    status: str
    checked_at: datetime = field(default_factory=utc_now)
    details: Mapping[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricPoint(SerializableModel):
    name: str
    value: float
    recorded_at: datetime = field(default_factory=utc_now)
    tags: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class TraceSpan(SerializableModel):
    name: str
    trace_id: str | None = None
    parent_span_id: str | None = None
    started_at: datetime = field(default_factory=utc_now)
    attributes: Mapping[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class FailureRecord(SerializableModel):
    message: str
    run_id: RunId | None = None
    event_id: EventId | None = None
    created_at: datetime = field(default_factory=utc_now)
    details: Mapping[str, JSONValue] = field(default_factory=dict)


ArtifactRef = Artifact
WorkspaceRef = Workspace


__all__ = [
    "AgentAction",
    "AgentPlan",
    "AgentPlanStep",
    "ApprovalDecision",
    "ApprovalRequest",
    "Artifact",
    "ArtifactRef",
    "CommandRequest",
    "CommandResult",
    "DeploymentRequest",
    "DeploymentResult",
    "FailureRecord",
    "FileSummary",
    "HealthCheckResult",
    "ImpactAnalysis",
    "JSONValue",
    "LLMMessage",
    "LLMResponse",
    "MetricPoint",
    "PatchProposal",
    "RepoContextRequest",
    "RepoContextResult",
    "Run",
    "RunEvent",
    "RunRequest",
    "RunResult",
    "SerializableModel",
    "Session",
    "SymbolMatch",
    "Task",
    "TokenUsage",
    "TraceSpan",
    "WatchSubscription",
    "Workspace",
    "WorkspaceRef",
    "utc_now",
]
