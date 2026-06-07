from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from .enums import (
    ActionKind,
    ApprovalMode,
    ArtifactType,
    DeploymentStatus,
    EventType,
    FailureCode,
    HealthState,
    RunMode,
    RunStatus,
)


@dataclass(slots=True)
class WorkspaceRef:
    workspace_id: str
    root_path: str
    branch: str | None = None
    commit_sha: str | None = None
    is_git_repo: bool | None = None


@dataclass(slots=True)
class ConstraintSet:
    approval_mode: ApprovalMode = ApprovalMode.ON_WRITE
    allow_shell: bool = True
    allow_network: bool = False
    allow_deploy: bool = False
    max_runtime_seconds: int | None = None
    max_output_bytes: int | None = None
    test_command: str | None = None
    deploy_target: str | None = None


@dataclass(slots=True)
class RunContext:
    session_id: str
    user_id: str | None = None
    branch: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunRequest:
    run_id: str
    workspace: WorkspaceRef
    context: RunContext
    mode: RunMode
    prompt: str
    targets: Sequence[str] = field(default_factory=tuple)
    constraints: ConstraintSet = field(default_factory=ConstraintSet)


@dataclass(slots=True)
class RepoContextRequest:
    run_id: str
    workspace: WorkspaceRef
    targets: Sequence[str] = field(default_factory=tuple)
    prompt: str | None = None
    include_repo_map: bool = True
    include_symbols: bool = True
    include_dependencies: bool = True


@dataclass(slots=True)
class FileSummary:
    path: str
    language: str | None = None
    summary: str | None = None
    token_estimate: int | None = None


@dataclass(slots=True)
class SymbolMatch:
    name: str
    kind: str
    path: str
    line: int | None = None
    score: float | None = None


@dataclass(slots=True)
class ImpactAnalysis:
    changed_files: Sequence[str] = field(default_factory=tuple)
    dependent_files: Sequence[str] = field(default_factory=tuple)
    symbols_at_risk: Sequence[str] = field(default_factory=tuple)
    warnings: Sequence[str] = field(default_factory=tuple)


@dataclass(slots=True)
class RepoContextResult:
    workspace: WorkspaceRef
    file_summaries: Sequence[FileSummary] = field(default_factory=tuple)
    symbols: Sequence[SymbolMatch] = field(default_factory=tuple)
    repo_map: str | None = None
    dependency_hints: Sequence[str] = field(default_factory=tuple)
    warnings: Sequence[str] = field(default_factory=tuple)


@dataclass(slots=True)
class AgentPlanStep:
    step_id: str
    title: str
    status: str = "pending"
    notes: str | None = None


@dataclass(slots=True)
class AgentPlan:
    run_id: str
    steps: Sequence[AgentPlanStep] = field(default_factory=tuple)
    rationale: str | None = None


@dataclass(slots=True)
class CommandRequest:
    run_id: str
    command_id: str
    command: str
    cwd: str
    timeout_seconds: int | None = None
    capture_output: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommandResult:
    run_id: str
    command_id: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int | None = None
    failure_code: FailureCode | None = None


@dataclass(slots=True)
class PatchProposal:
    run_id: str
    patch_id: str
    files: Sequence[str] = field(default_factory=tuple)
    diff: str | None = None
    summary: str | None = None
    requires_approval: bool = False


@dataclass(slots=True)
class ArtifactRef:
    artifact_id: str
    artifact_type: ArtifactType
    uri: str | None = None
    run_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ApprovalRequest:
    approval_id: str
    run_id: str
    reason: str
    files: Sequence[str] = field(default_factory=tuple)
    commands: Sequence[str] = field(default_factory=tuple)
    expires_at: datetime | None = None


@dataclass(slots=True)
class ApprovalDecision:
    approval_id: str
    run_id: str
    approved: bool
    reviewer_id: str | None = None
    comment: str | None = None


@dataclass(slots=True)
class AgentAction:
    run_id: str
    kind: ActionKind
    plan_step_id: str | None = None
    command_request: CommandRequest | None = None
    patch_proposal: PatchProposal | None = None
    approval_request: ApprovalRequest | None = None
    artifact_refs: Sequence[ArtifactRef] = field(default_factory=tuple)
    notes: str | None = None


@dataclass(slots=True)
class RunEvent:
    event_id: str
    run_id: str
    event_type: EventType
    status: RunStatus | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunResult:
    run_id: str
    status: RunStatus
    summary: str | None = None
    artifacts: Sequence[ArtifactRef] = field(default_factory=tuple)
    duration_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    failure_code: FailureCode | None = None
    warnings: Sequence[str] = field(default_factory=tuple)


@dataclass(slots=True)
class DeploymentRequest:
    deployment_id: str
    run_id: str
    workspace: WorkspaceRef
    target: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DeploymentResult:
    deployment_id: str
    run_id: str
    status: DeploymentStatus
    url: str | None = None
    logs_uri: str | None = None
    warnings: Sequence[str] = field(default_factory=tuple)


@dataclass(slots=True)
class HealthCheckResult:
    service: str
    state: HealthState
    checked_at: datetime = field(default_factory=datetime.utcnow)
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MetricPoint:
    name: str
    value: float
    timestamp: datetime = field(default_factory=datetime.utcnow)
    tags: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class TraceSpan:
    name: str
    trace_id: str | None = None
    parent_span_id: str | None = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FailureRecord:
    code: FailureCode
    message: str
    run_id: str | None = None
    event_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WatchSubscription:
    workspace_id: str
    subscription_id: str
    watched_paths: Sequence[str] = field(default_factory=tuple)


@dataclass(slots=True)
class LLMMessage:
    role: str
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass(slots=True)
class LLMResponse:
    provider: str
    model: str
    content: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str | None = None
