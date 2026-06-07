from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Sequence

from packages.shared_types import ApprovalDecision, RunEvent, RunRequest, RunResult
from services.agent_core import AgentCoreService
from services.execution_runtime import ExecutionRuntimeService
from services.ops_observability import OpsObservabilityService
from services.repo_intelligence import RepoIntelligenceService


class CLIApplication(ABC):
    """Thin CLI adapter over the platform services."""

    @abstractmethod
    async def run(self, argv: Sequence[str]) -> int:
        """Parse CLI arguments and dispatch the requested workflow."""

    @abstractmethod
    async def submit_run(self, request: RunRequest) -> str:
        """Create a new run and return its identifier."""

    @abstractmethod
    async def stream_run(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Yield live events for a run."""

    @abstractmethod
    async def await_result(self, run_id: str) -> RunResult:
        """Wait for a run to complete and return the final result."""

    @abstractmethod
    async def submit_approval(self, decision: ApprovalDecision) -> None:
        """Submit an approval decision for a blocked run."""


def create_cli_application(
    agent_core: AgentCoreService,
    execution_runtime: ExecutionRuntimeService,
    repo_intelligence: RepoIntelligenceService,
    observability: OpsObservabilityService,
) -> CLIApplication:
    """Create the CLI adapter from injected services."""
    raise NotImplementedError
