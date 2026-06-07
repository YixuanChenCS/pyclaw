from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Sequence

from packages.shared_types import (
    ApprovalDecision,
    ApprovalRequest,
    DeploymentRequest,
    DeploymentResult,
    HealthCheckResult,
    RunEvent,
    RunRequest,
    RunResult,
)
from services.agent_core import AgentCoreService
from services.execution_runtime import ExecutionRuntimeService
from services.ops_observability import OpsObservabilityService
from services.repo_intelligence import RepoIntelligenceService


class PlatformAPI(ABC):
    """Control-plane API for CLI and dashboard clients."""

    @abstractmethod
    async def create_run(self, request: RunRequest) -> str:
        """Persist and enqueue a new run."""

    @abstractmethod
    async def list_runs(self, workspace_id: str | None = None) -> Sequence[RunResult]:
        """List known runs, optionally filtered by workspace."""

    @abstractmethod
    async def get_run(self, run_id: str) -> RunResult | None:
        """Fetch the latest state for a run."""

    @abstractmethod
    async def stream_run_events(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Yield live and replayable events for a run."""

    @abstractmethod
    async def create_approval_request(self, request: ApprovalRequest) -> str:
        """Register a new approval checkpoint."""

    @abstractmethod
    async def submit_approval(self, decision: ApprovalDecision) -> None:
        """Resolve an approval request and resume execution."""

    @abstractmethod
    async def trigger_deployment(self, request: DeploymentRequest) -> DeploymentResult:
        """Start a deployment workflow for a completed run."""

    @abstractmethod
    async def get_health(self) -> HealthCheckResult:
        """Return readiness and dependency health for the control plane."""


def create_platform_api(
    agent_core: AgentCoreService,
    execution_runtime: ExecutionRuntimeService,
    repo_intelligence: RepoIntelligenceService,
    observability: OpsObservabilityService,
) -> PlatformAPI:
    """Create the platform API from injected services."""
    raise NotImplementedError
