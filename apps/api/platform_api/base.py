from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Sequence

from packages.shared_types import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    DeploymentRequest,
    DeploymentResult,
    HealthCheckResult,
    RecoveryStatus,
    RunEvent,
    RunRequest,
    RunResult,
    RunStatus,
)

from apps.api.schemas import ArtifactDetailResponse


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    artifact_id: str
    path: Path
    media_type: str
    filename: str


class PlatformAPI:
    """Control-plane API for CLI and dashboard clients."""

    async def create_run(self, request: RunRequest) -> str:
        raise NotImplementedError

    async def create_run_from_workspace(
        self,
        *,
        workspace_path: str,
        prompt: str,
        target_paths: Sequence[str] = (),
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        raise NotImplementedError

    async def list_runs(
        self,
        workspace_id: str | None = None,
        *,
        session_id: str | None = None,
        status: RunStatus | str | None = None,
        limit: int | None = None,
    ) -> Sequence[RunResult]:
        raise NotImplementedError

    async def get_run(self, run_id: str) -> RunResult | None:
        raise NotImplementedError

    async def get_run_summary(self, run_id: str) -> RunResult | None:
        raise NotImplementedError

    async def list_run_events(self, run_id: str) -> Sequence[RunEvent]:
        raise NotImplementedError

    async def list_artifacts(self, run_id: str) -> Sequence[ArtifactDetailResponse]:
        raise NotImplementedError

    async def get_artifact(self, artifact_id: str) -> ArtifactDetailResponse | None:
        raise NotImplementedError

    async def get_artifact_download(self, artifact_id: str) -> ArtifactDownload | None:
        raise NotImplementedError

    async def get_recovery_status(self, run_id: str) -> RecoveryStatus | None:
        raise NotImplementedError

    async def rollback_recovery(self, run_id: str, task_id: str) -> RecoveryStatus:
        raise NotImplementedError

    async def get_operability_snapshot(self) -> dict[str, object]:
        raise NotImplementedError

    async def list_approvals(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> Sequence[ApprovalRecord]:
        raise NotImplementedError

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        raise NotImplementedError

    async def stream_run_events(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
    ) -> AsyncIterator[RunEvent]:
        raise NotImplementedError

    async def create_approval_request(self, request: ApprovalRequest) -> str:
        raise NotImplementedError

    async def submit_approval(self, decision: ApprovalDecision) -> None:
        raise NotImplementedError

    async def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        comment: str | None = None,
    ) -> ApprovalRecord:
        raise NotImplementedError

    async def cancel_run(self, run_id: str) -> None:
        raise NotImplementedError

    async def trigger_deployment(self, request: DeploymentRequest) -> DeploymentResult:
        raise NotImplementedError

    async def get_health(self) -> HealthCheckResult:
        raise NotImplementedError

__all__ = ["ArtifactDownload", "PlatformAPI"]
