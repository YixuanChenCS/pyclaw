from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from packages.shared_types import ArtifactRef, RepoContextResult
from packages.shared_types.ids import RunId, WorkspaceId

from .model_client import ModelClient
from .models import AgentAction, AgentContextBudget, AgentFailure, AgentPlan, AgentSession


class AgentCoreService(ABC):
    """Headless agent orchestration service."""

    @abstractmethod
    def create_session(
        self,
        *,
        run_id: RunId,
        workspace_id: WorkspaceId,
        user_request: str,
        repo_context: RepoContextResult | None = None,
        current_plan: AgentPlan | None = None,
        prior_artifacts: Sequence[ArtifactRef] = (),
        iteration_count: int = 0,
        failure_history: Sequence[AgentFailure] = (),
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
    async def review_patch(
        self,
        session: AgentSession,
        proposed_action: AgentAction,
    ) -> AgentAction:
        """Review or refine a proposed patch action."""

    @abstractmethod
    async def summarize_run(self, session: AgentSession) -> AgentAction:
        """Produce the final structured summary/completion action."""
