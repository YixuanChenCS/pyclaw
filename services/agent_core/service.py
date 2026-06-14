from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from packages.shared_types import ArtifactRef, RepoContextResult
from packages.shared_types.ids import RunId, WorkspaceId

from .model_client import ModelClient
from .models import (
    AgentAction,
    AgentContextBudget,
    AgentFailure,
    AgentPlan,
    AgentSession,
    AgentSessionPhase,
    AgentVerification,
    PatchReview,
    RunSummary,
)


class AgentCoreService(ABC):
    """Headless agent orchestration service."""

    @abstractmethod
    def create_session(
        self,
        *,
        run_id: RunId,
        workspace_id: WorkspaceId,
        user_request: str,
        phase: AgentSessionPhase | None = None,
        repo_context: RepoContextResult | None = None,
        current_plan: AgentPlan | None = None,
        prior_artifacts: Sequence[ArtifactRef] = (),
        action_history: Sequence[AgentAction] = (),
        iteration_count: int = 0,
        failure_history: Sequence[AgentFailure] = (),
        verification_history: Sequence[AgentVerification] = (),
        warnings: Sequence[str] = (),
        context_budget: AgentContextBudget | None = None,
    ) -> AgentSession:
        """Construct deterministic session state for a run."""

    @property
    @abstractmethod
    def model_client(self) -> ModelClient | None:
        """Return the injected model-client abstraction, if configured."""

    @abstractmethod
    async def create_plan(self, session: AgentSession) -> AgentPlan:
        """Create a structured plan for the requested task."""

    @abstractmethod
    async def next_action(self, session: AgentSession) -> AgentAction:
        """Produce the next structured action without executing it."""

    @abstractmethod
    async def generate_command(
        self,
        session: AgentSession,
        proposed_action: AgentAction,
    ) -> AgentAction:
        """Generate a concrete command payload for a previously selected command action."""

    @abstractmethod
    async def generate_patch(
        self,
        session: AgentSession,
        proposed_action: AgentAction,
    ) -> AgentAction:
        """Generate a concrete patch proposal for a previously selected patch action."""

    @abstractmethod
    async def review_patch(
        self,
        session: AgentSession,
        proposed_action: AgentAction,
    ) -> PatchReview:
        """Review or refine a proposed patch action."""

    @abstractmethod
    def plan_patch_verification(
        self,
        session: AgentSession,
        *,
        changed_files: tuple[str, ...],
        deleted_files: tuple[str, ...] = (),
        workspace_root: str | None = None,
    ) -> tuple[AgentVerification, ...]:
        """Return deterministic verification steps for an applied patch, if needed."""

    @abstractmethod
    async def summarize_run(self, session: AgentSession) -> RunSummary:
        """Produce the final structured summary/completion action."""
