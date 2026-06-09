from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator, Sequence

from packages.shared_types import (
    ApprovalRequest,
    Artifact,
    ArtifactType,
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
    LockLease,
    PatchProposal,
    RepoStore,
    Run,
    RunEvent,
    RunId,
    RunRequest,
    RunResult,
    RunStatus,
    Workspace,
    WorkspaceLockManager,
    build_run_event,
    build_run_status_event,
    utc_now,
)

from .command import LocalCommandExecutor
from .events import validate_next_event_sequence
from .patch import LocalPatchApplier
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
        workspace_lock_manager: WorkspaceLockManager | None = None,
        db_path: str | Path | None = None,
        stream_poll_interval: float = 0.05,
    ) -> None:
        if repository is None:
            runtime_db_path = Path(db_path or ".execution_runtime/runtime.sqlite3")
            repository = SQLiteExecutionRuntimeRepository(runtime_db_path)
        self._repository = repository
        self._repo_store = repo_store
        self._workspace_lock_manager = workspace_lock_manager
        self._stream_poll_interval = stream_poll_interval
        self._startup_lock = asyncio.Lock()
        self._started = False
        self._active_command_tasks: dict[str, asyncio.Task[CommandResult]] = {}
        self._active_execution_tasks: dict[str, asyncio.Task[CommandResult]] = {}
        self._workspace_leases: dict[str, LockLease] = {}

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
        await self._ensure_started()
        typed_run_id = RunId(run_id)
        run = await self._repository.get_run(typed_run_id)
        if run is None:
            raise EntityNotFoundError("run", run_id)

        if run.status == RunStatus.QUEUED:
            await self._repository.update_run_status(
                run.run_id,
                RunStatus.CANCELLED,
                event_type=EventType.RUN_CANCELLED,
                message=reason,
                payload={"requested_reason": reason} if reason else None,
            )
            await self._release_workspace_lock(run_id)
            return

        if run.status in TERMINAL_RUN_STATUSES:
            raise InvalidRunStateError(
                f"Cannot cancel terminal run {run.run_id} in status {run.status.value}"
            )

        if run.status not in {RunStatus.RUNNING, RunStatus.WAITING_FOR_APPROVAL, RunStatus.CANCELLING}:
            raise InvalidRunStateError(
                f"Cannot cancel run {run.run_id} in status {run.status.value}"
            )

        if run.status != RunStatus.CANCELLING:
            await self._repository.update_run_status(
                run.run_id,
                RunStatus.CANCELLING,
                message=reason or "Cancellation requested.",
                payload={"requested_reason": reason} if reason else None,
            )

        active_task = self._active_command_tasks.get(run_id)
        execution_task = self._active_execution_tasks.get(run_id)
        if active_task is not None:
            active_task.cancel()
            await active_task
        if execution_task is not None:
            await execution_task

        await self._finalize_run_cancellation(run_id, reason=reason)
        await self._release_workspace_lock(run_id)

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
        run_id = str(request.run_id)
        if run_id in self._active_command_tasks or run_id in self._active_execution_tasks:
            raise InvalidRunStateError(f"Run {run.run_id} already has an active command.")

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
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("execute_command must run inside an asyncio task")

        self._active_execution_tasks[run_id] = current_task
        command_task = asyncio.create_task(executor.execute(request))
        self._active_command_tasks[run_id] = command_task
        try:
            result = await command_task
        finally:
            self._active_command_tasks.pop(run_id, None)
            self._active_execution_tasks.pop(run_id, None)

        latest_run = await self._repository.get_run(run.run_id)
        run_status = latest_run.status if latest_run is not None else run.status
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
                run_status=run_status,
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

        if result.cancelled:
            await self._finalize_run_cancellation(str(run.run_id), reason="Active command cancelled.")
            await self._release_workspace_lock(str(run.run_id))
        return result

    async def apply_patch(self, run_id: str, proposal: PatchProposal) -> ArtifactRef:
        await self._ensure_started()
        if str(proposal.run_id) != run_id:
            raise ErrorCodeContractError(
                ErrorCode.INVALID_REQUEST,
                "apply_patch run_id must match proposal.run_id.",
                details={"run_id": run_id, "proposal_run_id": str(proposal.run_id)},
            )

        run = await self._repository.get_run(proposal.run_id)
        if run is None:
            raise EntityNotFoundError("run", str(proposal.run_id))
        if run.status != RunStatus.RUNNING:
            raise InvalidRunStateError(
                f"Cannot apply patch for run {run.run_id} in status {run.status.value}"
            )

        workspace = await self._get_workspace(run)
        applier = LocalPatchApplier(workspace.root_path)
        try:
            changed_paths = applier.apply(proposal)
        except ErrorCodeContractError as exc:
            await self._repository.append_event_with_sequence(
                run.run_id,
                build_run_event(
                    run_id=run.run_id,
                    event_type=EventType.AGENT_MESSAGE,
                    task_id=proposal.task_id,
                    run_status=run.status,
                    message=str(exc),
                    payload={
                        "kind": "patch.failed",
                        "error_code": exc.error_code.value,
                    },
                ),
            )
            raise
        except Exception as exc:
            await self._repository.append_event_with_sequence(
                run.run_id,
                build_run_event(
                    run_id=run.run_id,
                    event_type=EventType.AGENT_MESSAGE,
                    task_id=proposal.task_id,
                    run_status=run.status,
                    message=str(exc),
                    payload={
                        "kind": "patch.failed",
                        "error_code": ErrorCode.PATCH_APPLY_FAILED.value,
                    },
                ),
            )
            raise ErrorCodeContractError(
                ErrorCode.PATCH_APPLY_FAILED,
                f"Patch application failed: {exc}",
            ) from exc

        artifact = Artifact(
            artifact_id=proposal.artifact_id,
            run_id=run.run_id,
            task_id=proposal.task_id,
            artifact_type=ArtifactType.PATCH,
            label=proposal.summary or "Patch applied",
            uri=",".join(changed_paths) if changed_paths else None,
        )
        await self._repository.create_artifact(artifact)
        await self._repository.append_event_with_sequence(
            run.run_id,
            build_run_event(
                run_id=run.run_id,
                event_type=EventType.PATCH_APPLIED,
                task_id=proposal.task_id,
                artifact_id=artifact.artifact_id,
                run_status=run.status,
                payload={
                    "artifact_id": str(artifact.artifact_id),
                    "target_paths": list(changed_paths),
                },
            ),
        )
        await self._repository.append_event_with_sequence(
            run.run_id,
            build_run_event(
                run_id=run.run_id,
                event_type=EventType.ARTIFACT_CREATED,
                task_id=proposal.task_id,
                artifact_id=artifact.artifact_id,
                run_status=run.status,
                payload={
                    "artifact_type": artifact.artifact_type.value,
                    "uri": artifact.uri,
                },
            ),
        )
        return artifact

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

    async def _finalize_run_cancellation(self, run_id: str, *, reason: str | None = None) -> None:
        run = await self._repository.get_run(run_id)
        if run is None:
            raise EntityNotFoundError("run", run_id)
        if run.status != RunStatus.CANCELLING:
            return

        try:
            await self._repository.update_run_status(
                run.run_id,
                RunStatus.CANCELLED,
                event_type=EventType.RUN_CANCELLED,
                message=reason or "Run cancelled.",
                payload={"requested_reason": reason} if reason else None,
            )
        except InvalidRunStateError:
            latest_run = await self._repository.get_run(run_id)
            if latest_run is None:
                raise EntityNotFoundError("run", run_id)
            if latest_run.status != RunStatus.CANCELLED:
                raise

    async def _release_workspace_lock(self, run_id: str) -> None:
        if self._workspace_lock_manager is None:
            return

        lease = self._workspace_leases.pop(run_id, None)
        if lease is None:
            return
        await self._workspace_lock_manager.release(lease)


__all__ = ["LocalExecutionRuntimeService"]
