from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from packages.shared_types import (
    ApprovalDecision,
    ApprovalRequest,
    ArtifactRef,
    ArtifactType,
    DeploymentRequest,
    DeploymentResult,
    EntityNotFoundError,
    ErrorCode,
    ErrorCodeContractError,
    EventType,
    InvalidRunStateError,
    CommandRequest,
    PatchProposal,
    RecoveryOption,
    RecoveryState,
    RecoveryStatus,
    RunRequest,
    RunResult,
    RunStatus,
    Session,
    Workspace,
    build_run_event,
    new_run_id,
    utc_now,
)
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class _InMemoryRepoStore:
    def __init__(self, workspaces: dict[str, Workspace] | None = None) -> None:
        self._workspaces = dict(workspaces or {})

    async def get_workspace(self, workspace_id):
        return self._workspaces.get(str(workspace_id))


class _DeploymentAdapter:
    def __init__(self) -> None:
        self.requests = []

    async def deploy(self, request):
        self.requests.append(request)
        return DeploymentResult(
            run_id=request.run_id,
            status="succeeded",
            url=f"https://deployments.example/{request.target}",
            started_at=utc_now(),
            finished_at=utc_now(),
        )


class TestLocalExecutionRuntimeService(unittest.IsolatedAsyncioTestCase):
    def _make_runtime(
        self,
        root: Path,
        *,
        workspaces: dict[str, Workspace] | None = None,
        deployment_adapter=None,
    ) -> tuple[LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository]:
        repository = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
        service = LocalExecutionRuntimeService(
            repository=repository,
            repo_store=_InMemoryRepoStore(workspaces),
            deployment_adapter=deployment_adapter,
            stream_poll_interval=0.01,
        )
        return service, repository

    def _make_request(self, workspace: Workspace | None = None) -> RunRequest:
        workspace = workspace or Workspace(root_path="/tmp/runtime-workspace")
        session = Session(workspace_id=workspace.workspace_id, title="runtime")
        return RunRequest(
            workspace_id=workspace.workspace_id,
            session_id=session.session_id,
            prompt="Implement runtime phase 1",
        )

    def _make_command_request(self, run_id: str, *, argv: tuple[str, ...], timeout_seconds=None) -> CommandRequest:
        return CommandRequest(
            run_id=run_id,
            task_id="task_command",
            argv=argv,
            timeout_seconds=timeout_seconds,
        )

    def _make_patch_proposal(
        self,
        run_id: str,
        *,
        unified_diff: str,
        target_paths: tuple[str, ...] = (),
        summary: str | None = "Apply patch",
    ) -> PatchProposal:
        return PatchProposal(
            run_id=run_id,
            task_id="task_patch",
            summary=summary,
            unified_diff=unified_diff,
            target_paths=target_paths,
        )

    def _make_approval_request(
        self,
        run_id: str,
        *,
        reason: str = "Need approval to continue",
        command_argv: tuple[str, ...] = (),
    ) -> ApprovalRequest:
        return ApprovalRequest(
            run_id=run_id,
            task_id="task_approval",
            reason=reason,
            command_argv=command_argv,
        )

    async def _collect_events(self, stream, count: int):
        collected = []
        try:
            for _ in range(count):
                collected.append(await asyncio.wait_for(anext(stream), timeout=1))
        finally:
            await stream.aclose()
        return collected

    async def test_enqueue_run_persists_queued_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            run = await repository.get_run(run_id)

            self.assertIsNotNone(run)
            self.assertEqual(run.status, RunStatus.QUEUED)
            self.assertEqual(run.attempt, 0)
            self.assertIsNone(run.worker_id)
            self.assertIsNone(run.lease_expires_at)
            self.assertIsNone(run.last_heartbeat_at)

    async def test_enqueue_run_emits_run_created_and_run_queued(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            events = await repository.list_events(run_id)

            self.assertEqual([event.event_type for event in events], [EventType.RUN_CREATED, EventType.RUN_QUEUED])
            self.assertEqual([event.sequence for event in events], [1, 2])

    async def test_enqueue_run_is_idempotent_for_same_explicit_run_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))
            run_id = new_run_id()
            request = replace(self._make_request(), run_id=run_id)

            first = await service.enqueue_run(request)
            second = await service.enqueue_run(request)
            events = await repository.list_events(run_id)

            self.assertEqual(first, second)
            self.assertEqual([event.sequence for event in events], [1, 2])

    async def test_enqueue_run_rejects_same_run_id_for_different_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _repository = self._make_runtime(Path(tmpdir))
            run_id = new_run_id()
            request = replace(self._make_request(), run_id=run_id)
            await service.enqueue_run(request)

            with self.assertRaises(ErrorCodeContractError) as context:
                await service.enqueue_run(replace(request, prompt="Different request"))

            self.assertEqual(context.exception.error_code, ErrorCode.INVALID_REQUEST)

    async def test_enqueue_run_rejects_same_run_id_with_different_target_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _repository = self._make_runtime(Path(tmpdir))
            run_id = new_run_id()
            request = replace(self._make_request(), run_id=run_id, target_paths=("app.py",))
            await service.enqueue_run(request)

            with self.assertRaises(ErrorCodeContractError) as context:
                await service.enqueue_run(replace(request, target_paths=("other.py",)))

            self.assertEqual(context.exception.error_code, ErrorCode.INVALID_REQUEST)

    async def test_event_sequence_is_strictly_increasing_per_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            claimed = await service.claim_next_run("worker-a", lease_seconds=30)
            self.assertIsNotNone(claimed)
            await repository.update_run_status(run_id, RunStatus.SUCCEEDED)

            events = await repository.list_events(run_id)
            self.assertEqual([event.sequence for event in events], [1, 2, 3, 4])

    async def test_stream_events_replays_historical_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            stream = service.stream_events(run_id)
            events = await self._collect_events(stream, 2)

            self.assertEqual([event.event_type for event in events], [EventType.RUN_CREATED, EventType.RUN_QUEUED])
            self.assertEqual([event.sequence for event in events], [1, 2])

    async def test_stream_events_replays_after_sequence_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            stream = service.stream_events(run_id, after_sequence=1)
            events = await self._collect_events(stream, 1)

            self.assertEqual([event.sequence for event in events], [2])

    async def test_stream_events_rejects_missing_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            stream = service.stream_events(run_id, after_sequence=99)
            with self.assertRaises(ErrorCodeContractError) as context:
                await anext(stream)

            self.assertEqual(context.exception.error_code, ErrorCode.EVENT_REPLAY_GAP)

    async def test_stream_events_raises_on_sequence_gap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            connection = sqlite3.connect(str(repository.db_path))
            try:
                connection.execute(
                    """
                    UPDATE run_events
                    SET sequence = 4
                    WHERE run_id = ? AND sequence = 2
                    """,
                    (run_id,),
                )
                connection.commit()
            finally:
                connection.close()

            stream = service.stream_events(run_id)
            first_event = await asyncio.wait_for(anext(stream), timeout=1)
            self.assertEqual(first_event.sequence, 1)

            with self.assertRaises(ErrorCodeContractError) as context:
                await asyncio.wait_for(anext(stream), timeout=1)

            await stream.aclose()
            self.assertEqual(context.exception.error_code, ErrorCode.EVENT_REPLAY_GAP)

    async def test_duplicate_claim_only_allows_one_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            first, second = await asyncio.gather(
                service.claim_next_run("worker-a", lease_seconds=30),
                service.claim_next_run("worker-b", lease_seconds=30),
            )

            claimed_runs = [run for run in (first, second) if run is not None]
            self.assertEqual(len(claimed_runs), 1)
            self.assertEqual(str(claimed_runs[0].run_id), run_id)
            self.assertIn(claimed_runs[0].worker_id, {"worker-a", "worker-b"})

            stored = await repository.get_run(run_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, RunStatus.RUNNING)
            self.assertEqual(stored.attempt, 1)
            self.assertEqual(stored.worker_id, claimed_runs[0].worker_id)

    async def test_same_workspace_second_run_is_not_claimed_while_first_is_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(Path(tmpdir))

            first_run_id = await service.enqueue_run(self._make_request(workspace))
            second_run_id = await service.enqueue_run(self._make_request(workspace))

            first_claim = await service.claim_next_run("worker-a", lease_seconds=30)
            second_claim = await service.claim_next_run("worker-b", lease_seconds=30)

            self.assertIsNotNone(first_claim)
            self.assertEqual(str(first_claim.run_id), first_run_id)
            self.assertIsNone(second_claim)

            stored_second = await repository.get_run(second_run_id)
            self.assertIsNotNone(stored_second)
            self.assertEqual(stored_second.status, RunStatus.QUEUED)

    async def test_claim_run_claims_the_requested_queued_run_instead_of_the_oldest_one(self):
        # Verifies that exact run claiming activates the requested run_id rather than whichever queued row happens to sort first.
        # This catches the lifecycle bug where CLI agent-patch could claim an older queued run and leave the newly created run stuck in queued.
        # The requested run is correct because claim_run takes an explicit run_id and should transition that exact row to running.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(Path(tmpdir))

            first_run_id = await service.enqueue_run(self._make_request(workspace))
            second_run_id = await service.enqueue_run(self._make_request(workspace))

            claimed = await service.claim_run(second_run_id, "worker-b", lease_seconds=30)
            first_run = await repository.get_run(first_run_id)
            second_run = await repository.get_run(second_run_id)

            self.assertIsNotNone(claimed)
            self.assertEqual(str(claimed.run_id), second_run_id)
            self.assertIsNotNone(first_run)
            self.assertEqual(first_run.status, RunStatus.QUEUED)
            self.assertIsNotNone(second_run)
            self.assertEqual(second_run.status, RunStatus.RUNNING)

    async def test_same_workspace_second_run_can_be_claimed_after_first_finalizes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(Path(tmpdir))

            first_run_id = await service.enqueue_run(self._make_request(workspace))
            second_run_id = await service.enqueue_run(self._make_request(workspace))

            first_claim = await service.claim_next_run("worker-a", lease_seconds=30)
            self.assertIsNotNone(first_claim)
            self.assertIsNone(await service.claim_next_run("worker-b", lease_seconds=30))

            await service.finalize_run(
                first_run_id,
                RunResult(
                    run_id=first_claim.run_id,
                    status=RunStatus.SUCCEEDED,
                    summary="done",
                ),
            )

            second_claim = await service.claim_next_run("worker-b", lease_seconds=30)
            self.assertIsNotNone(second_claim)
            self.assertEqual(str(second_claim.run_id), second_run_id)

            stored_second = await repository.get_run(second_run_id)
            self.assertIsNotNone(stored_second)
            self.assertEqual(stored_second.status, RunStatus.RUNNING)

    async def test_same_workspace_second_run_can_be_claimed_after_first_is_cancelled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(Path(tmpdir))

            first_run_id = await service.enqueue_run(self._make_request(workspace))
            second_run_id = await service.enqueue_run(self._make_request(workspace))

            first_claim = await service.claim_next_run("worker-a", lease_seconds=30)
            self.assertIsNotNone(first_claim)
            self.assertIsNone(await service.claim_next_run("worker-b", lease_seconds=30))

            await service.cancel_run(first_run_id, reason="release workspace")

            second_claim = await service.claim_next_run("worker-b", lease_seconds=30)
            self.assertIsNotNone(second_claim)
            self.assertEqual(str(second_claim.run_id), second_run_id)

            stored_second = await repository.get_run(second_run_id)
            self.assertIsNotNone(stored_second)
            self.assertEqual(stored_second.status, RunStatus.RUNNING)

    async def test_different_workspaces_can_be_claimed_independently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace_a_root = root / "ws-a"
            workspace_b_root = root / "ws-b"
            workspace_a_root.mkdir()
            workspace_b_root.mkdir()
            workspace_a = Workspace(root_path=str(workspace_a_root))
            workspace_b = Workspace(root_path=str(workspace_b_root))
            service, _repository = self._make_runtime(root)

            first_run_id = await service.enqueue_run(self._make_request(workspace_a))
            second_run_id = await service.enqueue_run(self._make_request(workspace_b))

            first_claim, second_claim = await asyncio.gather(
                service.claim_next_run("worker-a", lease_seconds=30),
                service.claim_next_run("worker-b", lease_seconds=30),
            )

            self.assertIsNotNone(first_claim)
            self.assertIsNotNone(second_claim)
            self.assertEqual(
                {str(first_claim.run_id), str(second_claim.run_id)},
                {first_run_id, second_run_id},
            )

    async def test_same_workspace_exclusion_is_durable_across_service_instances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            repository_a = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
            repository_b = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
            service_a = LocalExecutionRuntimeService(
                repository=repository_a,
                repo_store=_InMemoryRepoStore(),
                stream_poll_interval=0.01,
            )
            service_b = LocalExecutionRuntimeService(
                repository=repository_b,
                repo_store=_InMemoryRepoStore(),
                stream_poll_interval=0.01,
            )

            await service_a.enqueue_run(self._make_request(workspace))
            await service_a.enqueue_run(self._make_request(workspace))

            first_claim = await service_a.claim_next_run("worker-a", lease_seconds=30)
            second_claim = await service_b.claim_next_run("worker-b", lease_seconds=30)

            self.assertIsNotNone(first_claim)
            self.assertIsNone(second_claim)

    async def test_stale_running_runs_without_side_effects_are_requeued_on_startup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, repository = self._make_runtime(root)

            run_id = await service.enqueue_run(self._make_request())
            claimed = await service.claim_next_run("worker-a", lease_seconds=0)
            self.assertIsNotNone(claimed)
            await asyncio.sleep(0.02)

            restarted = LocalExecutionRuntimeService(
                repository=repository,
                stream_poll_interval=0.01,
            )
            await restarted.enqueue_run(self._make_request())

            recovered = await repository.get_run(run_id)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.status, RunStatus.QUEUED)
            self.assertIsNone(recovered.worker_id)
            self.assertIsNone(recovered.lease_expires_at)

            events = await repository.list_events(run_id)
            self.assertEqual(events[-1].event_type, EventType.RUN_QUEUED)
            self.assertEqual(events[-1].run_status, RunStatus.QUEUED)

    async def test_stale_running_run_after_patch_enters_explicit_recovery_state(self):
        # Verifies that a stale run after a durable patch side effect is surfaced as explicit recovery, not approval suspension.
        # This catches stale-run handling that hides replay risk behind a generic approval state.
        # needs_recovery is correct because the worker already performed side effects and the runtime must force structured recovery.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                root,
                workspaces={str(workspace.workspace_id): workspace},
            )
            file_path = root / "app.txt"
            file_path.write_text("before\n", encoding="utf-8")

            run_id = await service.enqueue_run(self._make_request(workspace))
            claimed = await service.claim_next_run("worker-a", lease_seconds=0)
            self.assertIsNotNone(claimed)
            await service.apply_patch(
                run_id,
                self._make_patch_proposal(
                    run_id,
                    unified_diff="--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                    target_paths=("app.txt",),
                ),
            )
            await asyncio.sleep(0.02)

            restarted = LocalExecutionRuntimeService(
                repository=repository,
                stream_poll_interval=0.01,
            )
            await restarted.enqueue_run(self._make_request(workspace))

            recovered = await repository.get_run(run_id)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.status, RunStatus.NEEDS_RECOVERY)
            self.assertIsNone(recovered.worker_id)
            self.assertIsNone(recovered.lease_expires_at)

            recovery = await service.get_recovery_status(run_id)
            self.assertIsNotNone(recovery)
            self.assertEqual(recovery.recovery_state, RecoveryState.ROLLBACK_AVAILABLE)
            self.assertIn(RecoveryOption.ROLLBACK_IF_AVAILABLE, recovery.recovery_options)

            stream = restarted.stream_events(run_id)
            events = await self._collect_events(stream, 8)
            self.assertEqual(events[-2].event_type, EventType.RUN_NEEDS_RECOVERY)
            self.assertEqual(events[-1].event_type, EventType.AGENT_MESSAGE)
            self.assertEqual(events[-1].payload["kind"], "manual_recovery_required")

    async def test_stale_running_run_after_command_start_enters_explicit_recovery_state(self):
        # Verifies that a stale run after command.started becomes explicit recovery, not an auto-resumable approval state.
        # This catches stale command recovery that would blur irreversible side effects into normal approval flow.
        # needs_recovery is correct because the runtime knows command execution began but has no safe terminal replay.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service, repository = self._make_runtime(root)

            run_id = await service.enqueue_run(self._make_request())
            claimed = await service.claim_next_run("worker-a", lease_seconds=0)
            self.assertIsNotNone(claimed)
            await repository.append_event_with_sequence(
                run_id,
                build_run_event(
                    run_id=claimed.run_id,
                    event_type=EventType.COMMAND_STARTED,
                    run_status=RunStatus.RUNNING,
                    task_id="task_command_recovery",
                    payload={"argv": ["python", "-m", "unittest"]},
                ),
            )
            await asyncio.sleep(0.02)

            restarted = LocalExecutionRuntimeService(
                repository=repository,
                stream_poll_interval=0.01,
            )
            await restarted.enqueue_run(self._make_request())

            recovered = await repository.get_run(run_id)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.status, RunStatus.NEEDS_RECOVERY)
            self.assertIsNone(recovered.worker_id)
            self.assertIsNone(recovered.lease_expires_at)

            recovery = await service.get_recovery_status(run_id)
            self.assertIsNotNone(recovery)
            self.assertEqual(recovery.recovery_state, RecoveryState.NEEDS_RECOVERY)
            self.assertNotIn(RecoveryOption.ROLLBACK_IF_AVAILABLE, recovery.recovery_options)

            events = await repository.list_events(run_id)
            self.assertEqual(events[-2].event_type, EventType.RUN_NEEDS_RECOVERY)
            self.assertEqual(events[-1].event_type, EventType.AGENT_MESSAGE)

    async def test_terminal_runs_cannot_transition_back_to_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            claimed = await service.claim_next_run("worker-a", lease_seconds=30)
            self.assertIsNotNone(claimed)
            await repository.update_run_status(run_id, RunStatus.SUCCEEDED)

            with self.assertRaises(InvalidRunStateError):
                await repository.update_run_status(run_id, RunStatus.RUNNING)

    async def test_execute_command_emits_started_and_completed_for_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            result = await service.execute_command(
                self._make_command_request(
                    run_id,
                    argv=("sh", "-c", "printf success"),
                )
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout, "success")
            events = await repository.list_events(run_id)
            self.assertEqual(events[-2].event_type, EventType.COMMAND_STARTED)
            self.assertEqual(events[-1].event_type, EventType.COMMAND_COMPLETED)

    async def test_execute_command_emits_failed_for_non_zero_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            result = await service.execute_command(
                self._make_command_request(
                    run_id,
                    argv=("sh", "-c", "exit 7"),
                )
            )

            self.assertEqual(result.exit_code, 7)
            self.assertEqual(result.termination_reason, "exit_code")
            events = await repository.list_events(run_id)
            self.assertEqual(events[-1].event_type, EventType.COMMAND_FAILED)

    async def test_execute_command_emits_timeout_for_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            result = await service.execute_command(
                self._make_command_request(
                    run_id,
                    argv=("sh", "-c", "sleep 1"),
                    timeout_seconds=0.05,
                )
            )

            self.assertTrue(result.timed_out)
            self.assertEqual(result.termination_reason, "timeout")
            events = await repository.list_events(run_id)
            self.assertEqual(events[-1].event_type, EventType.COMMAND_TIMEOUT)

    async def test_stream_events_replays_command_events_after_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, _repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            await service.execute_command(
                self._make_command_request(
                    run_id,
                    argv=("sh", "-c", "printf replay"),
                )
            )

            stream = service.stream_events(run_id)
            events = await self._collect_events(stream, 5)
            self.assertEqual(
                [event.event_type for event in events],
                [
                    EventType.RUN_CREATED,
                    EventType.RUN_QUEUED,
                    EventType.RUN_STARTED,
                    EventType.COMMAND_STARTED,
                    EventType.COMMAND_COMPLETED,
                ],
            )

    async def test_execute_command_does_not_finalize_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            await service.execute_command(
                self._make_command_request(
                    run_id,
                    argv=("sh", "-c", "printf stay-running"),
                )
            )

            run = await repository.get_run(run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.status, RunStatus.RUNNING)
            self.assertIsNone(run.finished_at)

    async def test_request_approval_persists_approval_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            approval_request = self._make_approval_request(
                run_id,
                command_argv=("git", "push"),
            )
            approval_id = await service.request_approval(run_id, approval_request)

            self.assertEqual(approval_id, str(approval_request.approval_id))
            approvals = await repository.list_approval_requests(run_id)
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0].approval_id, approval_request.approval_id)
            self.assertEqual(approvals[0].reason, approval_request.reason)
            self.assertEqual(approvals[0].command_argv, ("git", "push"))

    async def test_approval_decision_rejects_expired_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, _repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            request = replace(
                self._make_approval_request(run_id),
                expires_at=utc_now() - timedelta(seconds=1),
            )
            await service.request_approval(run_id, request)

            with self.assertRaises(ErrorCodeContractError) as context:
                await service.record_approval_decision(
                    ApprovalDecision(
                        approval_id=request.approval_id,
                        run_id=request.run_id,
                        approved=True,
                    )
                )

            self.assertEqual(context.exception.error_code, ErrorCode.APPROVAL_EXPIRED)

    async def test_list_runs_filters_by_workspace_session_and_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))
            first_request = self._make_request()
            second_request = self._make_request()
            first_run_id = await service.enqueue_run(first_request)
            await service.enqueue_run(second_request)
            await repository.update_run_status(first_run_id, RunStatus.CANCELLED)

            runs = await repository.list_runs(
                first_request.workspace_id,
                session_id=first_request.session_id,
                status=RunStatus.CANCELLED,
            )

            self.assertEqual([str(run.run_id) for run in runs], [first_run_id])

    async def test_attach_artifacts_persists_and_replays_same_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))
            run_id = await service.enqueue_run(self._make_request())
            artifact = ArtifactRef(
                run_id=run_id,
                artifact_type=ArtifactType.LOG,
                label="deployment log",
                uri="memory://deployment.log",
            )

            await service.attach_artifacts(run_id, (artifact,))
            await service.attach_artifacts(run_id, (artifact,))

            persisted = await repository.list_artifacts(run_id)
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0].artifact_id, artifact.artifact_id)

    async def test_deploy_uses_configured_adapter_for_succeeded_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = _DeploymentAdapter()
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
                deployment_adapter=adapter,
            )
            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            await repository.update_run_status(run_id, RunStatus.SUCCEEDED)
            request = DeploymentRequest(
                run_id=run_id,
                workspace_id=workspace.workspace_id,
                target="staging",
            )

            result = await service.deploy(request)

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(adapter.requests, [request])

    async def test_health_reports_runtime_storage_queue_and_locks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _repository = self._make_runtime(Path(tmpdir))
            await service.enqueue_run(self._make_request())

            health = await service.get_health()

            self.assertEqual(health.status, "ready")
            self.assertEqual(health.details["db"], "ready")
            self.assertEqual(health.details["artifact_store"], "ready")
            self.assertEqual(health.details["queue"]["queued"], 1)
            self.assertEqual(health.details["locks"]["status"], "disabled")

    async def test_request_approval_transitions_running_to_waiting_for_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            await service.request_approval(run_id, self._make_approval_request(run_id))

            run = await repository.get_run(run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.status, RunStatus.WAITING_FOR_APPROVAL)
            self.assertIsNone(run.worker_id)
            self.assertIsNone(run.lease_expires_at)

    async def test_request_approval_event_is_replayable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, _repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            approval_request = self._make_approval_request(run_id)
            await service.claim_next_run("worker-a", lease_seconds=30)
            await service.request_approval(run_id, approval_request)

            stream = service.stream_events(run_id)
            events = await self._collect_events(stream, 4)
            self.assertEqual(
                [event.event_type for event in events],
                [
                    EventType.RUN_CREATED,
                    EventType.RUN_QUEUED,
                    EventType.RUN_STARTED,
                    EventType.APPROVAL_REQUESTED,
                ],
            )
            self.assertEqual(events[-1].approval_id, approval_request.approval_id)

    async def test_request_approval_reuses_existing_request_for_same_task_id(self):
        # Verifies that repeating the same approval task_id replays the original approval instead of creating a duplicate checkpoint.
        # This catches duplicate durable approval rows/events after resume or retried orchestration calls.
        # Replay is correct because the first approval request is already the canonical side effect for that task_id.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            approval_request = self._make_approval_request(
                run_id,
                reason="Need approval to continue",
                command_argv=("git", "push"),
            )

            first = await service.request_approval(run_id, approval_request)
            second = await service.request_approval(run_id, approval_request)
            approvals = await repository.list_approval_requests(run_id)
            events = await repository.list_events(run_id)

            self.assertEqual(first, second)
            self.assertEqual(len(approvals), 1)
            self.assertEqual(
                [event.event_type for event in events].count(EventType.APPROVAL_REQUESTED),
                1,
            )

    async def test_request_approval_does_not_finalize_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            await service.request_approval(run_id, self._make_approval_request(run_id))

            run = await repository.get_run(run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.status, RunStatus.WAITING_FOR_APPROVAL)
            self.assertIsNone(run.finished_at)

    async def test_terminal_runs_cannot_request_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            await repository.update_run_status(run_id, RunStatus.SUCCEEDED)

            with self.assertRaises(InvalidRunStateError):
                await service.request_approval(run_id, self._make_approval_request(run_id))

    async def test_request_approval_raises_for_non_existent_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, _repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            with self.assertRaises(EntityNotFoundError):
                await service.request_approval(
                    "run_missing",
                    self._make_approval_request("run_missing"),
                )

    async def test_cancel_queued_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            await service.cancel_run(run_id, reason="user requested")

            run = await repository.get_run(run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.status, RunStatus.CANCELLED)
            events = await repository.list_events(run_id)
            self.assertEqual(events[-1].event_type, EventType.RUN_CANCELLED)

    async def test_cancel_running_run_with_no_active_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            await service.claim_next_run("worker-a", lease_seconds=30)
            await service.cancel_run(run_id, reason="stop now")

            run = await repository.get_run(run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.status, RunStatus.CANCELLED)
            events = await repository.list_events(run_id)
            self.assertEqual(events[-2].event_type, EventType.RUN_STATUS_CHANGED)
            self.assertEqual(events[-2].run_status, RunStatus.CANCELLING)
            self.assertEqual(events[-1].event_type, EventType.RUN_CANCELLED)

    async def test_cancel_running_run_while_command_is_active(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            command_task = asyncio.create_task(
                service.execute_command(
                    self._make_command_request(
                        run_id,
                        argv=(sys.executable, "-c", "import time; time.sleep(5)"),
                    )
                )
            )

            for _ in range(100):
                events = await repository.list_events(run_id)
                if any(event.event_type == EventType.COMMAND_STARTED for event in events):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("command.started was not persisted before cancellation")

            await service.cancel_run(run_id, reason="interrupt active command")
            result = await command_task

            self.assertTrue(result.cancelled)
            self.assertEqual(result.termination_reason, "cancelled")
            run = await repository.get_run(run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.status, RunStatus.CANCELLED)

            events = await repository.list_events(run_id)
            event_types = [event.event_type for event in events]
            self.assertIn(EventType.RUN_STATUS_CHANGED, event_types)
            self.assertIn(EventType.COMMAND_CANCELLED, event_types)
            self.assertEqual(events[-1].event_type, EventType.RUN_CANCELLED)

    async def test_terminal_run_cannot_be_cancelled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            await service.claim_next_run("worker-a", lease_seconds=30)
            await repository.update_run_status(run_id, RunStatus.SUCCEEDED)

            with self.assertRaises(InvalidRunStateError):
                await service.cancel_run(run_id)

    async def test_stream_events_replays_cancellation_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service, _repository = self._make_runtime(Path(tmpdir))

            run_id = await service.enqueue_run(self._make_request())
            await service.cancel_run(run_id, reason="replay cancel")

            stream = service.stream_events(run_id)
            events = await self._collect_events(stream, 3)
            self.assertEqual(
                [event.event_type for event in events],
                [
                    EventType.RUN_CREATED,
                    EventType.RUN_QUEUED,
                    EventType.RUN_CANCELLED,
                ],
            )

    async def test_apply_patch_modifies_file_in_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            file_path = Path(tmpdir) / "app.txt"
            file_path.write_text("before\n", encoding="utf-8")

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            artifact = await service.apply_patch(
                run_id,
                self._make_patch_proposal(
                    run_id,
                    unified_diff="--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                    target_paths=("app.txt",),
                ),
            )

            self.assertEqual(file_path.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(artifact.artifact_type, ArtifactType.PATCH)
            artifacts = await repository.list_artifacts(run_id)
            self.assertEqual(len(artifacts), 1)

    async def test_apply_patch_rejects_path_escape_with_dotdot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, _repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)

            with self.assertRaises(ErrorCodeContractError) as context:
                await service.apply_patch(
                    run_id,
                    self._make_patch_proposal(
                        run_id,
                        unified_diff="--- a/../escape.txt\n+++ b/../escape.txt\n@@ -0,0 +1 @@\n+bad\n",
                        target_paths=("../escape.txt",),
                    ),
                )

            self.assertEqual(context.exception.error_code, ErrorCode.AGENT_WRITE_OUTSIDE_WORKSPACE)

    async def test_apply_patch_rejects_absolute_path_outside_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            outside_file = Path(tmpdir).parent / "outside-patch.txt"
            service, _repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)

            with self.assertRaises(ErrorCodeContractError) as context:
                await service.apply_patch(
                    run_id,
                    self._make_patch_proposal(
                        run_id,
                        unified_diff=(
                            f"--- {outside_file}\n+++ {outside_file}\n@@ -0,0 +1 @@\n+bad\n"
                        ),
                        target_paths=(str(outside_file),),
                    ),
                )

            self.assertEqual(context.exception.error_code, ErrorCode.AGENT_WRITE_OUTSIDE_WORKSPACE)

    async def test_apply_patch_detects_conflict_when_file_drifted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, _repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            file_path = Path(tmpdir) / "app.txt"
            file_path.write_text("current\n", encoding="utf-8")

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            with self.assertRaises(ErrorCodeContractError) as context:
                await service.apply_patch(
                    run_id,
                    self._make_patch_proposal(
                        run_id,
                        unified_diff="--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                        target_paths=("app.txt",),
                    ),
                )

            self.assertEqual(context.exception.error_code, ErrorCode.PATCH_CONFLICT)
            self.assertEqual(file_path.read_text(encoding="utf-8"), "current\n")

    async def test_apply_patch_persists_patch_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            file_path = Path(tmpdir) / "app.txt"
            file_path.write_text("before\n", encoding="utf-8")

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            artifact = await service.apply_patch(
                run_id,
                self._make_patch_proposal(
                    run_id,
                    unified_diff="--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                    target_paths=("app.txt",),
                    summary="Rename contents",
                ),
            )

            artifacts = await repository.list_artifacts(run_id)
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0].artifact_id, artifact.artifact_id)
            self.assertEqual(artifacts[0].label, "Rename contents")
            self.assertEqual(artifacts[0].uri, "app.txt")

    async def test_apply_patch_emits_replayable_patch_event(self):
        # Verifies that patch application now emits a durable patch-started marker before patch/applied artifact events.
        # This catches regressions where resume would lose the evidence that a patch side effect had already begun.
        # The extra agent.message event is correct because patch.started is now the guardrail used for safe replay detection.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, _repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            file_path = Path(tmpdir) / "app.txt"
            file_path.write_text("before\n", encoding="utf-8")

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            await service.apply_patch(
                run_id,
                self._make_patch_proposal(
                    run_id,
                    unified_diff="--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                    target_paths=("app.txt",),
                ),
            )

            stream = service.stream_events(run_id)
            events = await self._collect_events(stream, 6)
            self.assertEqual(
                [event.event_type for event in events],
                [
                    EventType.RUN_CREATED,
                    EventType.RUN_QUEUED,
                    EventType.RUN_STARTED,
                    EventType.AGENT_MESSAGE,
                    EventType.PATCH_APPLIED,
                    EventType.ARTIFACT_CREATED,
                ],
            )
            self.assertEqual(events[3].payload["kind"], "patch.started")

    async def test_apply_patch_does_not_finalize_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            file_path = Path(tmpdir) / "app.txt"
            file_path.write_text("before\n", encoding="utf-8")

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            await service.apply_patch(
                run_id,
                self._make_patch_proposal(
                    run_id,
                    unified_diff="--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                    target_paths=("app.txt",),
                ),
            )

            run = await repository.get_run(run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.status, RunStatus.RUNNING)
            self.assertIsNone(run.finished_at)

    async def test_apply_patch_creates_new_file_from_dev_null_diff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, _repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            await service.apply_patch(
                run_id,
                self._make_patch_proposal(
                    run_id,
                    unified_diff="--- /dev/null\n+++ b/new_file.txt\n@@ -0,0 +1 @@\n+created\n",
                    target_paths=("new_file.txt",),
                ),
            )

            self.assertEqual((Path(tmpdir) / "new_file.txt").read_text(encoding="utf-8"), "created\n")

    async def test_apply_patch_handles_multiple_hunks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, _repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            file_path = Path(tmpdir) / "app.txt"
            file_path.write_text("one\nkeep\nthree\nstay\n", encoding="utf-8")

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            await service.apply_patch(
                run_id,
                self._make_patch_proposal(
                    run_id,
                    unified_diff=(
                        "--- a/app.txt\n"
                        "+++ b/app.txt\n"
                        "@@ -1,2 +1,2 @@\n"
                        "-one\n"
                        "+ONE\n"
                        " keep\n"
                        "@@ -3,2 +3,2 @@\n"
                        "-three\n"
                        "+THREE\n"
                        " stay\n"
                    ),
                    target_paths=("app.txt",),
                ),
            )

            self.assertEqual(file_path.read_text(encoding="utf-8"), "ONE\nkeep\nTHREE\nstay\n")

    async def test_apply_patch_preserves_no_newline_markers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, _repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            file_path = Path(tmpdir) / "app.txt"
            file_path.write_text("before", encoding="utf-8")

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            await service.apply_patch(
                run_id,
                self._make_patch_proposal(
                    run_id,
                    unified_diff=(
                        "--- a/app.txt\n"
                        "+++ b/app.txt\n"
                        "@@ -1 +1 @@\n"
                        "-before\n"
                        "\\ No newline at end of file\n"
                        "+after\n"
                        "\\ No newline at end of file\n"
                    ),
                    target_paths=("app.txt",),
                ),
            )

            self.assertEqual(file_path.read_text(encoding="utf-8"), "after")

    async def test_execute_command_reuses_existing_result_for_same_task_id(self):
        # Verifies that rerunning the same command task_id replays the stored result instead of executing side effects twice.
        # This catches duplicate command execution after resume when the coordinator dispatches the same saved action again.
        # The replayed result is correct because the first terminal command event is already the canonical outcome for that task_id.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            request = self._make_command_request(run_id, argv=("sh", "-c", "printf once"))

            first = await service.execute_command(request)
            second = await service.execute_command(request)
            events = await repository.list_events(run_id)

            self.assertEqual(first.exit_code, 0)
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(second.stdout, "")
            self.assertEqual(
                [event.event_type for event in events].count(EventType.COMMAND_STARTED),
                1,
            )
            self.assertEqual(
                [event.event_type for event in events].count(EventType.COMMAND_COMPLETED),
                1,
            )

    async def test_execute_command_fails_loudly_if_same_task_only_started(self):
        # Verifies that a half-finished command task cannot be silently replayed as if it were safe.
        # This catches resume paths that would rerun side effects after a crash between command.started and completion.
        # Failing is correct because the runtime has evidence the task already started but no terminal outcome to replay.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            claimed = await service.claim_next_run("worker-a", lease_seconds=30)
            self.assertIsNotNone(claimed)
            request = self._make_command_request(run_id, argv=("sh", "-c", "printf once"))
            await repository.append_event_with_sequence(
                run_id,
                build_run_event(
                    run_id=claimed.run_id,
                    event_type=EventType.COMMAND_STARTED,
                    run_status=RunStatus.RUNNING,
                    task_id=request.task_id,
                    payload={"argv": list(request.argv)},
                ),
            )

            with self.assertRaises(InvalidRunStateError):
                await service.execute_command(request)

            recovery = await service.get_recovery_status(run_id)
            run = await repository.get_run(run_id)

            self.assertIsNotNone(recovery)
            self.assertEqual(run.status, RunStatus.NEEDS_RECOVERY)
            self.assertEqual(recovery.recovery_state, RecoveryState.NEEDS_RECOVERY)
            self.assertEqual(
                recovery.recovery_options,
                (RecoveryOption.REVIEW_MANUALLY, RecoveryOption.ABORT),
            )

    async def test_apply_patch_reuses_existing_artifact_for_same_task_id(self):
        # Verifies that rerunning the same patch task_id returns the stored artifact instead of mutating files twice.
        # This catches duplicate patch application after resume when the same saved patch action is dispatched again.
        # The replayed artifact is correct because the first persisted patch artifact is the canonical result for that task_id.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            file_path = Path(tmpdir) / "app.txt"
            file_path.write_text("before\n", encoding="utf-8")

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            proposal = self._make_patch_proposal(
                run_id,
                unified_diff="--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                target_paths=("app.txt",),
            )

            first = await service.apply_patch(run_id, proposal)
            second = await service.apply_patch(run_id, proposal)
            artifacts = await repository.list_artifacts(run_id)
            events = await repository.list_events(run_id)

            self.assertEqual(first.artifact_id, second.artifact_id)
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(file_path.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(
                [event for event in events if event.event_type == EventType.PATCH_APPLIED].__len__(),
                1,
            )

    async def test_apply_patch_fails_loudly_if_same_task_only_started(self):
        # Verifies that a patch task with only a durable started marker cannot be replayed as a fresh patch apply.
        # This catches resume paths that would apply the same destructive patch twice after a crash mid-application.
        # Failing is correct because the runtime knows patch.started happened but has no artifact proving safe completion.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            file_path = Path(tmpdir) / "app.txt"
            file_path.write_text("before\n", encoding="utf-8")

            run_id = await service.enqueue_run(self._make_request(workspace))
            claimed = await service.claim_next_run("worker-a", lease_seconds=30)
            self.assertIsNotNone(claimed)
            proposal = self._make_patch_proposal(
                run_id,
                unified_diff="--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                target_paths=("app.txt",),
            )
            await repository.append_event_with_sequence(
                run_id,
                build_run_event(
                    run_id=claimed.run_id,
                    event_type=EventType.AGENT_MESSAGE,
                    run_status=RunStatus.RUNNING,
                    task_id=proposal.task_id,
                    payload={"kind": "patch.started"},
                ),
            )

            with self.assertRaises(InvalidRunStateError):
                await service.apply_patch(run_id, proposal)

            recovery = await service.get_recovery_status(run_id)
            run = await repository.get_run(run_id)

            self.assertIsNotNone(recovery)
            self.assertEqual(run.status, RunStatus.NEEDS_RECOVERY)
            self.assertEqual(recovery.recovery_state, RecoveryState.ROLLBACK_AVAILABLE)
            self.assertEqual(recovery.rollback_task_id, proposal.task_id)
            self.assertIn(RecoveryOption.ROLLBACK_IF_AVAILABLE, recovery.recovery_options)

    async def test_rollback_task_restores_patch_snapshot_and_requires_manual_review(self):
        # Verifies that patch snapshots can restore the workspace to its pre-patch state after recovery is required.
        # This catches rollback implementations that mark recovery complete without actually undoing filesystem mutation.
        # The restored file and rollback-required-review state are correct because undo should revert side effects but still force human review.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )
            file_path = Path(tmpdir) / "app.txt"
            file_path.write_text("before\n", encoding="utf-8")

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            proposal = self._make_patch_proposal(
                run_id,
                unified_diff="--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                target_paths=("app.txt",),
            )
            await repository.save_patch_snapshot(
                run_id=run_id,
                task_id=proposal.task_id,
                relative_path="app.txt",
                existed_before=True,
                content="before\n",
            )
            file_path.write_text("after\n", encoding="utf-8")
            await repository.append_event_with_sequence(
                run_id,
                build_run_event(
                    run_id=run_id,
                    event_type=EventType.AGENT_MESSAGE,
                    run_status=RunStatus.RUNNING,
                    task_id=proposal.task_id,
                    payload={"kind": "patch.started"},
                ),
            )
            await repository.update_run_status(
                run_id,
                RunStatus.NEEDS_RECOVERY,
                event_type=EventType.RUN_NEEDS_RECOVERY,
                message="Patch started and interrupted.",
            )
            await repository.upsert_recovery_status(
                RecoveryStatus(
                    run_id=run_id,
                    task_id=proposal.task_id,
                    recovery_state=RecoveryState.ROLLBACK_AVAILABLE,
                    reason="Patch task interrupted.",
                    recovery_options=(
                        RecoveryOption.ROLLBACK_IF_AVAILABLE,
                        RecoveryOption.REVIEW_MANUALLY,
                        RecoveryOption.ABORT,
                    ),
                    rollback_task_id=proposal.task_id,
                )
            )

            recovery = await service.rollback_task(run_id, str(proposal.task_id))
            events = await repository.list_events(run_id)

            self.assertEqual(file_path.read_text(encoding="utf-8"), "before\n")
            self.assertEqual(recovery.recovery_state, RecoveryState.ROLLBACK_REQUIRED_REVIEW)
            self.assertEqual(
                recovery.recovery_options,
                (RecoveryOption.REVIEW_MANUALLY, RecoveryOption.ABORT),
            )
            self.assertEqual(events[-1].payload["kind"], "rollback.completed")

    async def test_finalize_run_is_idempotent_for_same_terminal_status(self):
        # Verifies that replaying the same terminal finalize request is a no-op instead of raising or rewriting state.
        # This catches duplicate complete dispatch after resume when finalize_run already succeeded once.
        # The unchanged event count is correct because the persisted terminal status is already the canonical final state.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            claimed = await service.claim_next_run("worker-a", lease_seconds=30)
            self.assertIsNotNone(claimed)
            result = RunResult(run_id=claimed.run_id, status=RunStatus.SUCCEEDED, summary="done")

            await service.finalize_run(run_id, result)
            before_events = await repository.list_events(run_id)
            await service.finalize_run(run_id, result)
            after_events = await repository.list_events(run_id)

            self.assertEqual(len(before_events), len(after_events))
            self.assertEqual((await repository.get_run(run_id)).status, RunStatus.SUCCEEDED)

    async def test_record_approval_decision_updates_row_and_resume_run_transitions_back_to_running(self):
        # Verifies that approval decisions are durably recorded and that an approval pause can be resumed explicitly.
        # This catches approval flows that look accepted in memory but never update the durable runtime state.
        # The running status is correct because the run was waiting solely on approval and has now been resumed.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            service, repository = self._make_runtime(
                Path(tmpdir),
                workspaces={str(workspace.workspace_id): workspace},
            )

            run_id = await service.enqueue_run(self._make_request(workspace))
            await service.claim_next_run("worker-a", lease_seconds=30)
            approval_request = self._make_approval_request(run_id)
            await service.request_approval(run_id, approval_request)
            await service.record_approval_decision(
                ApprovalDecision(
                    approval_id=approval_request.approval_id,
                    run_id=approval_request.run_id,
                    approved=True,
                    reviewer="human",
                    comment="looks safe",
                )
            )
            await service.resume_run(run_id)

            approvals = await repository.list_approval_requests(run_id)
            run = await repository.get_run(run_id)
            events = await repository.list_events(run_id)

            self.assertEqual(len(approvals), 1)
            self.assertEqual(run.status, RunStatus.RUNNING)
            self.assertEqual(events[-2].event_type, EventType.APPROVAL_RESOLVED)
            self.assertEqual(events[-1].event_type, EventType.RUN_STATUS_CHANGED)


if __name__ == "__main__":
    unittest.main()
