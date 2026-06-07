from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from packages.shared_types import (
    AgentAction,
    AgentPlan,
    ArtifactRef,
    PatchProposal,
    RepoContextResult,
    RunRequest,
    RunResult,
)


class AgentCoreService(ABC):
    """Headless agent orchestration service."""

    @abstractmethod
    async def create_plan(self, request: RunRequest, repo_context: RepoContextResult) -> AgentPlan:
        """Create a structured plan for the requested task."""

    @abstractmethod
    async def next_action(
        self,
        request: RunRequest,
        repo_context: RepoContextResult,
        prior_artifacts: Sequence[ArtifactRef],
    ) -> AgentAction:
        """Produce the next action in the run state machine."""

    @abstractmethod
    async def review_patch(
        self,
        request: RunRequest,
        repo_context: RepoContextResult,
        proposal: PatchProposal,
    ) -> PatchProposal:
        """Review or refine a patch before application."""

    @abstractmethod
    async def summarize_run(
        self,
        request: RunRequest,
        repo_context: RepoContextResult,
        artifacts: Sequence[ArtifactRef],
    ) -> RunResult:
        """Create the final run result."""
