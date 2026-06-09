from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest

from packages.shared_types import (
    ErrorCode,
    ErrorCodeContractError,
    EventType,
    InvalidRunStateError,
    RunRequest,
    RunStatus,
    Session,
    Workspace,
)
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class TestLocalExecutionRuntimeService(unittest.IsolatedAsyncioTestCase):
    def _make_runtime(
        self,
        root: Path,
    ) -> tuple[LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository]:
        repository = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
        service = LocalExecutionRuntimeService(
            repository=repository,
            stream_poll_interval=0.01,
        )
        return service, repository

    def _make_request(self) -> RunRequest:
        workspace = Workspace(root_path="/tmp/runtime-workspace")
        session = Session(workspace_id=workspace.workspace_id, title="runtime")
        return RunRequest(
            workspace_id=workspace.workspace_id,
            session_id=session.session_id,
            prompt="Implement runtime phase 1",
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


if __name__ == "__main__":
    unittest.main()
