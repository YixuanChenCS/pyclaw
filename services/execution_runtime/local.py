from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator, Sequence

from packages.shared_types import (
    ApprovalRequest,
    ArtifactRef,
    CommandRequest,
    CommandResult,
    DeploymentRequest,
    DeploymentResult,
    EntityNotFoundError,
    EventType,
    PatchProposal,
    Run,
    RunEvent,
    RunId,
    RunRequest,
    RunResult,
    build_run_status_event,
    utc_now,
)

from .events import validate_next_event_sequence
from .service import ExecutionRuntimeService
from .sqlite_store import SQLiteExecutionRuntimeRepository
from .state_machine import TERMINAL_RUN_STATUSES


class LocalExecutionRuntimeService(ExecutionRuntimeService):
    """Durable local runtime with SQLite-backed queue and event replay."""

    def __init__(
        self,
        *,
        repository: SQLiteExecutionRuntimeRepository | None = None,
        db_path: str | Path | None = None,
        stream_poll_interval: float = 0.05,
    ) -> None:
        if repository is None:
            runtime_db_path = Path(db_path or ".execution_runtime/runtime.sqlite3")
            repository = SQLiteExecutionRuntimeRepository(runtime_db_path)
        self._repository = repository
        self._stream_poll_interval = stream_poll_interval
        self._startup_lock = asyncio.Lock()
        self._started = False

    @property
    def repository(self) -> SQLiteExecutionRuntimeRepository:
        return self._repository

    async def enqueue_run(self, request: RunRequest) -> str:
        await self._ensure_started()
        run = Run(
            run_id=request.run_id or RunId.generate(),
            workspace_id=request.workspace_id,
            session_id=request.session_id,
            prompt=request.prompt,
        )
        await self._repository.create_run(
            run,
            events=(
                build_run_status_event(run, EventType.RUN_CREATED),
                build_run_status_event(run, EventType.RUN_QUEUED),
            ),
        )
        return str(run.run_id)

    async def cancel_run(self, run_id: str, reason: str | None = None) -> None:
        raise NotImplementedError("Phase 1 does not implement cancellation.")

    async def stream_events(self, run_id: str) -> AsyncIterator[RunEvent]:
        await self._ensure_started()
        current_sequence = 0
        typed_run_id = RunId(run_id)
        while True:
            events = await self._repository.list_events(typed_run_id, after_sequence=current_sequence)
            for event in events:
                current_sequence = validate_next_event_sequence(run_id, current_sequence, event)
                yield event

            run = await self._repository.get_run(typed_run_id)
            if run is None:
                raise EntityNotFoundError("run", run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return
            await asyncio.sleep(self._stream_poll_interval)

    async def claim_next_run(self, worker_id: str, lease_seconds: int) -> Run | None:
        await self._ensure_started()
        return await self._repository.claim_next_run(worker_id, lease_seconds)

    async def execute_command(self, request: CommandRequest) -> CommandResult:
        raise NotImplementedError("Phase 1 does not implement command execution.")

    async def apply_patch(self, run_id: str, proposal: PatchProposal) -> ArtifactRef:
        raise NotImplementedError("Phase 1 does not implement patch application.")

    async def request_approval(self, run_id: str, request: ApprovalRequest) -> str:
        raise NotImplementedError("Phase 1 does not implement approval checkpoints.")

    async def attach_artifacts(self, run_id: str, artifacts: Sequence[ArtifactRef]) -> None:
        raise NotImplementedError("Phase 1 does not implement artifact persistence.")

    async def finalize_run(self, run_id: str, result: RunResult) -> None:
        raise NotImplementedError("Phase 1 does not implement run finalization.")

    async def deploy(self, request: DeploymentRequest) -> DeploymentResult:
        raise NotImplementedError("Phase 1 does not implement deployment.")

    async def _ensure_started(self) -> None:
        if self._started:
            return
        async with self._startup_lock:
            if self._started:
                return
            await self._repository.recover_stale_runs(utc_now())
            self._started = True


__all__ = ["LocalExecutionRuntimeService"]
