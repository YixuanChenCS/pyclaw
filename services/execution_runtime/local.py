from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import AsyncIterator, Sequence

from packages.shared_types import (
    ApprovalDecision,
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
    RecoveryOption,
    RecoveryState,
    RecoveryStatus,
    RepoStore,
    Run,
    RunEvent,
    RunId,
    RunRequest,
    RunResult,
    RunStatus,
    TaskId,
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

    _PATCH_HEADER_RE = re.compile(r"^(---|\+\+\+)\s+(.+)$")

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

        if run.status not in {
            RunStatus.RUNNING,
            RunStatus.WAITING_FOR_APPROVAL,
            RunStatus.NEEDS_RECOVERY,
            RunStatus.CANCELLING,
        }:
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

        replayed = await self._replay_command_result(request)
        if replayed is not None:
            return replayed

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

        replayed_artifact = await self._replay_patch_artifact(proposal)
        if replayed_artifact is not None:
            return replayed_artifact

        run = await self._repository.get_run(proposal.run_id)
        if run is None:
            raise EntityNotFoundError("run", str(proposal.run_id))
        if run.status != RunStatus.RUNNING:
            raise InvalidRunStateError(
                f"Cannot apply patch for run {run.run_id} in status {run.status.value}"
            )

        workspace = await self._get_workspace(run)
        await self._capture_patch_snapshots(workspace.root_path, proposal)
        applier = LocalPatchApplier(workspace.root_path)
        await self._repository.append_event_with_sequence(
            run.run_id,
            build_run_event(
                run_id=run.run_id,
                event_type=EventType.AGENT_MESSAGE,
                task_id=proposal.task_id,
                run_status=run.status,
                message="Patch application started.",
                payload={"kind": "patch.started"},
            ),
        )
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
        await self._ensure_started()
        if str(request.run_id) != run_id:
            raise ErrorCodeContractError(
                ErrorCode.INVALID_REQUEST,
                "request_approval run_id must match request.run_id.",
                details={"run_id": run_id, "request_run_id": str(request.run_id)},
            )

        replayed_approval_id = await self._replay_approval_request(request)
        if replayed_approval_id is not None:
            return replayed_approval_id

        run = await self._repository.get_run(request.run_id)
        if run is None:
            raise EntityNotFoundError("run", str(request.run_id))
        if run.status != RunStatus.RUNNING:
            raise InvalidRunStateError(
                f"Cannot request approval for run {run.run_id} in status {run.status.value}"
            )

        await self._repository.create_approval_request(request)
        await self._release_workspace_lock(run_id)
        return str(request.approval_id)

    async def record_approval_decision(self, decision: ApprovalDecision) -> None:
        await self._ensure_started()
        await self._repository.update_approval_decision(decision)

    async def get_recovery_status(self, run_id: str) -> RecoveryStatus | None:
        await self._ensure_started()
        return await self._repository.get_recovery_status(run_id)

    async def rollback_task(self, run_id: str, task_id: str) -> RecoveryStatus:
        await self._ensure_started()
        typed_run_id = RunId(run_id)
        run = await self._repository.get_run(typed_run_id)
        if run is None:
            raise EntityNotFoundError("run", run_id)

        typed_task_id = TaskId(task_id)
        snapshots = await self._repository.list_patch_snapshots(typed_run_id, typed_task_id)
        if not snapshots:
            raise InvalidRunStateError(f"No rollback snapshot exists for task {task_id}")

        workspace = await self._get_workspace(run)
        root = Path(workspace.root_path).resolve(strict=True)
        for snapshot in snapshots:
            path = (root / str(snapshot["relative_path"])).resolve(strict=False)
            if not path.is_relative_to(root):
                raise InvalidRunStateError(
                    f"Rollback path escapes workspace: {snapshot['relative_path']}"
                )
            if bool(snapshot["existed_before"]):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(snapshot["content"] or ""), encoding="utf-8")
            elif path.exists():
                path.unlink()

        recovery = RecoveryStatus(
            run_id=typed_run_id,
            task_id=typed_task_id,
            recovery_state=RecoveryState.ROLLBACK_REQUIRED_REVIEW,
            reason="Rollback completed; manual review is required before continuing the run.",
            recovery_options=(
                RecoveryOption.REVIEW_MANUALLY,
                RecoveryOption.ABORT,
            ),
            rollback_task_id=typed_task_id,
        )
        await self._repository.upsert_recovery_status(recovery)
        await self._repository.append_event_with_sequence(
            typed_run_id,
            build_run_event(
                run_id=typed_run_id,
                event_type=EventType.AGENT_MESSAGE,
                run_status=RunStatus.NEEDS_RECOVERY,
                task_id=typed_task_id,
                message=recovery.reason,
                payload={
                    "kind": "rollback.completed",
                    "recovery_state": recovery.recovery_state.value,
                    "rollback_task_id": str(typed_task_id),
                },
            ),
        )
        return recovery

    async def attach_artifacts(self, run_id: str, artifacts: Sequence[ArtifactRef]) -> None:
        raise NotImplementedError("Phase 1 does not implement artifact persistence.")

    async def finalize_run(self, run_id: str, result: RunResult) -> None:
        await self._ensure_started()
        if str(result.run_id) != run_id:
            raise ErrorCodeContractError(
                ErrorCode.INVALID_REQUEST,
                "finalize_run run_id must match result.run_id.",
                details={"run_id": run_id, "result_run_id": str(result.run_id)},
            )

        run = await self._repository.get_run(result.run_id)
        if run is None:
            raise EntityNotFoundError("run", str(result.run_id))
        if run.status in TERMINAL_RUN_STATUSES:
            if run.status == result.status:
                return
            raise InvalidRunStateError(
                f"Cannot finalize terminal run {run.run_id} from {run.status.value} to {result.status.value}"
            )
        if result.status not in TERMINAL_RUN_STATUSES:
            raise ErrorCodeContractError(
                ErrorCode.INVALID_REQUEST,
                f"finalize_run requires a terminal status, got {result.status.value}.",
                details={"status": result.status.value},
            )

        payload = {}
        if result.summary is not None:
            payload["summary"] = result.summary
        if result.error_message is not None:
            payload["error_message"] = result.error_message
        if result.artifacts:
            payload["artifact_ids"] = [str(artifact.artifact_id) for artifact in result.artifacts]

        message = result.summary or result.error_message
        await self._repository.update_run_status(
            run.run_id,
            result.status,
            message=message,
            payload=payload or None,
        )
        await self._release_workspace_lock(run_id)

    async def resume_run(self, run_id: str) -> None:
        await self._ensure_started()
        typed_run_id = RunId(run_id)
        run = await self._repository.get_run(typed_run_id)
        if run is None:
            raise EntityNotFoundError("run", run_id)
        if run.status == RunStatus.RUNNING:
            return
        if run.status != RunStatus.WAITING_FOR_APPROVAL:
            raise InvalidRunStateError(
                f"Cannot resume run {run.run_id} in status {run.status.value}"
            )

        await self._repository.update_run_status(
            run.run_id,
            RunStatus.RUNNING,
            event_type=EventType.RUN_STATUS_CHANGED,
            message="Run resumed after approval.",
            payload={"kind": "approval_resume"},
        )

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

    async def _replay_command_result(self, request: CommandRequest) -> CommandResult | None:
        events = await self._repository.list_events(request.run_id)
        started_event = None
        terminal_event = None
        for event in events:
            if str(event.task_id or "") != str(request.task_id):
                continue
            if event.event_type == EventType.COMMAND_STARTED:
                started_event = event
            if event.event_type in {
                EventType.COMMAND_COMPLETED,
                EventType.COMMAND_FAILED,
                EventType.COMMAND_TIMEOUT,
                EventType.COMMAND_CANCELLED,
            }:
                terminal_event = event

        if terminal_event is not None:
            payload = dict(terminal_event.payload)
            return CommandResult(
                run_id=request.run_id,
                task_id=request.task_id,
                exit_code=payload.get("exit_code"),
                timed_out=bool(payload.get("timed_out", False)),
                cancelled=bool(payload.get("cancelled", False)),
                stdout_truncated=bool(payload.get("stdout_truncated", False)),
                stderr_truncated=bool(payload.get("stderr_truncated", False)),
                termination_reason=payload.get("termination_reason"),
                started_at=started_event.created_at if started_event is not None else None,
                finished_at=terminal_event.created_at,
            )

        if started_event is not None:
            await self._mark_recovery_required(
                request.run_id,
                task_id=request.task_id,
                recovery_state=RecoveryState.NEEDS_RECOVERY,
                reason=(
                    f"Command task {request.task_id} already started and cannot be replayed safely."
                ),
                recovery_options=(
                    RecoveryOption.REVIEW_MANUALLY,
                    RecoveryOption.ABORT,
                ),
            )
            raise InvalidRunStateError(
                f"Command task {request.task_id} already started and cannot be replayed safely"
            )

        return None

    async def _replay_patch_artifact(self, proposal: PatchProposal) -> ArtifactRef | None:
        artifacts = await self._repository.list_artifacts(proposal.run_id)
        for artifact in artifacts:
            if artifact.artifact_type != ArtifactType.PATCH:
                continue
            if str(artifact.task_id or "") != str(proposal.task_id or ""):
                continue
            return artifact

        events = await self._repository.list_events(proposal.run_id)
        for event in events:
            if str(event.task_id or "") != str(proposal.task_id or ""):
                continue
            if event.event_type == EventType.AGENT_MESSAGE and event.payload.get("kind") == "patch.started":
                await self._mark_recovery_required(
                    proposal.run_id,
                    task_id=proposal.task_id,
                    recovery_state=RecoveryState.ROLLBACK_AVAILABLE,
                    reason=(
                        f"Patch task {proposal.task_id} already started and cannot be replayed safely."
                    ),
                    recovery_options=(
                        RecoveryOption.ROLLBACK_IF_AVAILABLE,
                        RecoveryOption.REVIEW_MANUALLY,
                        RecoveryOption.ABORT,
                    ),
                    rollback_task_id=proposal.task_id,
                )
                raise InvalidRunStateError(
                    f"Patch task {proposal.task_id} already started and requires manual recovery"
                )

        return None

    async def _replay_approval_request(self, request: ApprovalRequest) -> str | None:
        if request.task_id is None:
            return None

        approvals = await self._repository.list_approval_requests(request.run_id)
        for approval in approvals:
            if str(approval.task_id or "") != str(request.task_id):
                continue
            if (
                approval.reason != request.reason
                or approval.command_argv != request.command_argv
                or approval.patch_id != request.patch_id
            ):
                raise InvalidRunStateError(
                    f"Approval task {request.task_id} already exists with conflicting request details"
                )
            return str(approval.approval_id)

        return None

    async def _capture_patch_snapshots(self, workspace_root: str, proposal: PatchProposal) -> None:
        if proposal.task_id is None:
            return

        root = Path(workspace_root).resolve(strict=True)
        for target_path in self._patch_target_paths(proposal):
            path = (root / target_path).resolve(strict=False)
            if not path.is_relative_to(root):
                raise ErrorCodeContractError(
                    ErrorCode.AGENT_WRITE_OUTSIDE_WORKSPACE,
                    f"Patch target must stay inside workspace: {target_path}",
                    details={
                        "workspace_root": str(root),
                        "resolved_path": str(path),
                    },
                )
            existed_before = path.exists()
            content = path.read_text(encoding="utf-8") if existed_before else None
            await self._repository.save_patch_snapshot(
                run_id=proposal.run_id,
                task_id=proposal.task_id,
                relative_path=target_path,
                existed_before=existed_before,
                content=content,
            )

    def _patch_target_paths(self, proposal: PatchProposal) -> tuple[str, ...]:
        if proposal.target_paths:
            return tuple(proposal.target_paths)

        paths: list[str] = []
        old_path: str | None = None
        for line in proposal.unified_diff.splitlines():
            match = self._PATCH_HEADER_RE.match(line)
            if match is None:
                continue
            marker, raw_path = match.groups()
            normalized = self._normalize_patch_header_path(raw_path)
            if marker == "---":
                old_path = normalized
                continue
            if marker == "+++":
                if normalized == "/dev/null":
                    if old_path is None or old_path == "/dev/null":
                        raise InvalidRunStateError("Patch diff cannot delete /dev/null")
                    paths.append(old_path)
                else:
                    paths.append(normalized)
        return tuple(dict.fromkeys(paths))

    def _normalize_patch_header_path(self, raw_path: str) -> str:
        path = raw_path.strip()
        if path.startswith(("a/", "b/")) and path != "/dev/null":
            return path[2:]
        return path

    async def _mark_recovery_required(
        self,
        run_id: RunId,
        *,
        task_id: TaskId | None,
        recovery_state: RecoveryState,
        reason: str,
        recovery_options: tuple[RecoveryOption, ...],
        rollback_task_id: TaskId | None = None,
    ) -> None:
        run = await self._repository.get_run(run_id)
        if run is None:
            raise EntityNotFoundError("run", str(run_id))

        recovery = RecoveryStatus(
            run_id=run_id,
            task_id=task_id,
            recovery_state=recovery_state,
            reason=reason,
            recovery_options=recovery_options,
            rollback_task_id=rollback_task_id,
        )
        await self._repository.upsert_recovery_status(recovery)
        if run.status != RunStatus.NEEDS_RECOVERY:
            await self._repository.update_run_status(
                run_id,
                RunStatus.NEEDS_RECOVERY,
                event_type=EventType.RUN_NEEDS_RECOVERY,
                message=reason,
                payload={
                    "recovery_state": recovery.recovery_state.value,
                    "task_id": str(task_id) if task_id is not None else None,
                    "rollback_task_id": str(rollback_task_id) if rollback_task_id is not None else None,
                    "recovery_options": [option.value for option in recovery_options],
                },
            )


__all__ = ["LocalExecutionRuntimeService"]
