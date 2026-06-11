from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Sequence

from packages.shared_types import (
    ApprovalRequest,
    ArtifactRef,
    CommandRequest,
    CommandResult,
    DeploymentRequest,
    DeploymentResult,
    PatchProposal,
    RecoveryStatus,
    RunEvent,
    RunRequest,
    RunResult,
)


class ExecutionRuntimeService(ABC):
    """Async runtime for queued runs and isolated tool execution."""

    @abstractmethod
    async def enqueue_run(self, request: RunRequest) -> str:
        """Persist and queue a new run."""

    @abstractmethod
    async def cancel_run(self, run_id: str, reason: str | None = None) -> None:
        """Cancel a queued or active run."""

    @abstractmethod
    async def stream_events(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Yield durable events for a run."""

    @abstractmethod
    async def execute_command(self, request: CommandRequest) -> CommandResult:
        """Execute a shell or tool command in the runtime sandbox."""

    @abstractmethod
    async def apply_patch(self, run_id: str, proposal: PatchProposal) -> ArtifactRef:
        """Apply a patch proposal and return the resulting artifact."""

    @abstractmethod
    async def request_approval(self, run_id: str, request: ApprovalRequest) -> str:
        """Register an approval checkpoint for the active run."""

    @abstractmethod
    async def attach_artifacts(self, run_id: str, artifacts: Sequence[ArtifactRef]) -> None:
        """Attach artifacts to the run record."""

    @abstractmethod
    async def finalize_run(self, run_id: str, result: RunResult) -> None:
        """Mark the run complete and persist its final result."""

    @abstractmethod
    async def get_recovery_status(self, run_id: str) -> RecoveryStatus | None:
        """Return the current structured recovery state for the run, if one exists."""

    @abstractmethod
    async def rollback_task(self, run_id: str, task_id: str) -> RecoveryStatus:
        """Rollback a recoverable task and return the resulting recovery state."""

    @abstractmethod
    async def deploy(self, request: DeploymentRequest) -> DeploymentResult:
        """Run the deployment workflow for a completed run."""
