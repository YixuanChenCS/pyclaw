from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from packages.shared_types import (
    ArtifactRef,
    CommandResult,
    RecoveryStatus,
    RepoContextResult,
    TaskStatus,
    utc_now,
)
from packages.shared_types.ids import RunId, WorkspaceId
from packages.shared_types.models import SerializableModel


class AgentActionType(str, Enum):
    ASK_CONTEXT = "ask_context"
    RUN_COMMAND = "run_command"
    PROPOSE_PATCH = "propose_patch"
    REQUEST_APPROVAL = "request_approval"
    SUMMARIZE = "summarize"
    COMPLETE = "complete"


class AgentSessionPhase(str, Enum):
    PLANNING = "planning"
    READY = "ready"
    EXECUTING = "executing"
    AWAITING_CONTEXT = "awaiting_context"
    AWAITING_APPROVAL = "awaiting_approval"
    NEEDS_RECOVERY = "needs_recovery"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentFailure(SerializableModel):
    stage: str
    message: str
    code: str | None = None
    retryable: bool = False
    details: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentContextBudget(SerializableModel):
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    remaining_input_tokens: int | None = None
    remaining_output_tokens: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentStep(SerializableModel):
    kind: str
    description: str
    step_id: str | None = None
    target_files: tuple[str, ...] = ()
    rationale: str | None = None
    status: TaskStatus = TaskStatus.PENDING


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentPlan(SerializableModel):
    goal: str
    steps: list[AgentStep] = field(default_factory=list)
    summary: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentAction(SerializableModel):
    type: AgentActionType
    reason: str
    step_id: str | None = None
    action_id: str | None = None
    target_files: tuple[str, ...] = ()
    command_argv: tuple[str, ...] = ()
    cwd: str | None = None
    patch_diff: str | None = None
    allow_file_deletions: bool = False
    approval_message: str | None = None
    approval_risk_reason: str | None = None
    summary_text: str | None = None
    requested_context: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PatchReview(SerializableModel):
    accepted: bool
    reason: str
    changed_files: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    patch_diff: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RunSummary(SerializableModel):
    final_status: str
    completed_steps: tuple[str, ...] = ()
    attempted_actions: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    commands_run: tuple[tuple[str, ...], ...] = ()
    checks_passed: bool | None = None
    warnings: tuple[str, ...] = ()
    unfinished_items: tuple[str, ...] = ()
    failure_messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class LoopGuardResult(SerializableModel):
    triggered: bool
    guard_kind: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentRunOutcome(SerializableModel):
    status: str
    session: AgentSession
    last_action: AgentAction | None = None
    summary: RunSummary | None = None
    patch_review: PatchReview | None = None
    command_result: CommandResult | None = None
    approval_id: str | None = None
    recovery_status: RecoveryStatus | None = None
    requested_context: tuple[str, ...] = ()
    applied_artifacts: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSession(SerializableModel):
    run_id: RunId
    workspace_id: WorkspaceId
    user_request: str
    phase: AgentSessionPhase = AgentSessionPhase.PLANNING
    repo_context: RepoContextResult | None = None
    current_plan: AgentPlan | None = None
    prior_artifacts: list[ArtifactRef] = field(default_factory=list)
    action_history: list[AgentAction] = field(default_factory=list)
    pending_action: AgentAction | None = None
    pending_approval_id: str | None = None
    completed_action_ids: list[str] = field(default_factory=list)
    iteration_count: int = 0
    failure_history: list[AgentFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    context_budget: AgentContextBudget | None = None
    created_at: datetime = field(default_factory=utc_now)
