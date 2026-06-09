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
    ErrorCode,
    ErrorCodeContractError,
    EventType,
    InvalidRunStateError,
    PatchProposal,
    RepoStore,
    Run,
    RunEvent,
    RunId,
    RunRequest,
    RunResult,
    RunStatus,
    Workspace,
    build_run_event,
    build_run_status_event,
    utc_now,
)

from .command import LocalCommandExecutor
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
        repo_store: RepoStore | None = None,
        db_path: str | Path | None = None,
        stream_poll_interval: float = 0.05,
    ) -> None:
        if repository is None:
            runtime_db_path = Path(db_path or ".execution_runtime/runtime.sqlite3")
            repository = SQLiteExecutionRuntimeRepository(runtime_db_path)
        self._repository = repository
        self._repo_store = repo_store
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
        await self._ensure_started()
        if not request.argv:
            raise ErrorCodeContractError(
                ErrorCode.INVALID_REQUEST,
                "Command argv must not be empty.",
            )

        run = await self._repository.get_run(request.run_id)
        if run is None:
            raise EntityNotFoundError("run", str(request.run_id))
        if run.status != RunStatus.RUNNING:
            raise InvalidRunStateError(
                f"Cannot execute command for run {run.run_id} in status {run.status.value}"
            )

        workspace = await self._get_workspace(run)
        executor = LocalCommandExecutor(workspace.root_path)

        await self._repository.append_event_with_sequence(
            run.run_id,
            build_run_event(
                run_id=run.run_id,
                event_type=EventType.COMMAND_STARTED,
                task_id=request.task_id,
                run_status=run.status,
                payload={
                    "argv": list(request.argv),
                    "cwd": request.cwd,
                    "timeout_seconds": request.timeout_seconds,
                },
            ),
        )
        result = await executor.execute(request)

        event_type = EventType.COMMAND_COMPLETED
        if result.cancelled:
            event_type = EventType.COMMAND_CANCELLED
        elif result.timed_out:
            event_type = EventType.COMMAND_TIMEOUT
        elif result.exit_code not in (None, 0):
            event_type = EventType.COMMAND_FAILED

        await self._repository.append_event_with_sequence(
            run.run_id,
            build_run_event(
                run_id=run.run_id,
                event_type=event_type,
                task_id=request.task_id,
                run_status=run.status,
                payload={
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "cancelled": result.cancelled,
                    "stdout_truncated": result.stdout_truncated,
                    "stderr_truncated": result.stderr_truncated,
                    "termination_reason": result.termination_reason,
                },
            ),
        )
        return result

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

    async def _get_workspace(self, run: Run) -> Workspace:
        if self._repo_store is None:
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_NOT_FOUND,
                f"Workspace store is not configured for run {run.run_id}.",
                details={"workspace_id": str(run.workspace_id)},
            )

        workspace = await self._repo_store.get_workspace(run.workspace_id)
        if workspace is None:
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_NOT_FOUND,
                f"Workspace not found for run {run.run_id}.",
                details={"workspace_id": str(run.workspace_id)},
            )
        return workspace


__all__ = ["LocalExecutionRuntimeService"]
