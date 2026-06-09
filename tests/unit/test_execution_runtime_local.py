from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from packages.shared_types import (
    ArtifactType,
    ErrorCode,
    ErrorCodeContractError,
    EventType,
    InvalidRunStateError,
    CommandRequest,
    PatchProposal,
    RunRequest,
    RunStatus,
    Session,
    Workspace,
)
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class _InMemoryRepoStore:
    def __init__(self, workspaces: dict[str, Workspace] | None = None) -> None:
        self._workspaces = dict(workspaces or {})

    async def get_workspace(self, workspace_id):
        return self._workspaces.get(str(workspace_id))


class TestLocalExecutionRuntimeService(unittest.IsolatedAsyncioTestCase):
    def _make_runtime(
        self,
        root: Path,
        *,
        workspaces: dict[str, Workspace] | None = None,
    ) -> tuple[LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository]:
        repository = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
        service = LocalExecutionRuntimeService(
            repository=repository,
            repo_store=_InMemoryRepoStore(workspaces),
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

    async def test_stale_running_runs_are_recovered_on_startup(self):
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
            events = await self._collect_events(stream, 5)
            self.assertEqual(
                [event.event_type for event in events],
                [
                    EventType.RUN_CREATED,
                    EventType.RUN_QUEUED,
                    EventType.RUN_STARTED,
                    EventType.PATCH_APPLIED,
                    EventType.ARTIFACT_CREATED,
                ],
            )

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


if __name__ == "__main__":
    unittest.main()
