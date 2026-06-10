from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from packages.shared_types import ArtifactRef, RepoContextResult, TaskStatus, utc_now
from packages.shared_types.ids import RunId, WorkspaceId
from packages.shared_types.models import SerializableModel


class AgentActionType(str, Enum):
    ASK_CONTEXT = "ask_context"
    RUN_COMMAND = "run_command"
    PROPOSE_PATCH = "propose_patch"
    REQUEST_APPROVAL = "request_approval"
    SUMMARIZE = "summarize"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentFailure(SerializableModel):
    stage: str
    message: str
    retryable: bool = False


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
    command_argv: tuple[str, ...] = ()
    cwd: str | None = None
    patch_diff: str | None = None
    approval_message: str | None = None
    approval_risk_reason: str | None = None
    summary_text: str | None = None
    requested_context: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSession(SerializableModel):
    run_id: RunId
    workspace_id: WorkspaceId
    user_request: str
    repo_context: RepoContextResult | None = None
    current_plan: AgentPlan | None = None
    prior_artifacts: list[ArtifactRef] = field(default_factory=list)
    iteration_count: int = 0
    failure_history: list[AgentFailure] = field(default_factory=list)
    context_budget: AgentContextBudget | None = None
    created_at: datetime = field(default_factory=utc_now)
