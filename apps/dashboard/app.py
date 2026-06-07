from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Mapping, Sequence

from packages.shared_types import ApprovalDecision, DeploymentResult, RunEvent, RunRequest, RunResult
from services.ops_observability import OpsObservabilityService


class DashboardApplication(ABC):
    """Thin dashboard client over the platform API."""

    @abstractmethod
    async def load_home(self) -> Mapping[str, Any]:
        """Load initial dashboard state."""

    @abstractmethod
    async def load_run(self, run_id: str) -> RunResult | None:
        """Load a run detail view model."""

    @abstractmethod
    async def load_runs(self, workspace_id: str | None = None) -> Sequence[RunResult]:
        """Load run summaries for the dashboard list view."""

    @abstractmethod
    async def submit_run(self, request: RunRequest) -> str:
        """Create a run through the API client."""

    @abstractmethod
    async def stream_run(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Subscribe to live run events."""

    @abstractmethod
    async def submit_approval(self, decision: ApprovalDecision) -> None:
        """Approve or reject a pending action."""

    @abstractmethod
    async def load_deployment(self, deployment_id: str) -> DeploymentResult | None:
        """Load deployment status and health information."""


def create_dashboard_application(observability: OpsObservabilityService) -> DashboardApplication:
    """Create the dashboard adapter from injected dependencies."""
    raise NotImplementedError
