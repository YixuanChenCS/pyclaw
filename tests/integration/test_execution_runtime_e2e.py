from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from packages.shared_types import (
    CommandRequest,
    EventType,
    PatchProposal,
    RunRequest,
    RunResult,
    RunStatus,
    Session,
    Workspace,
)
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class _InMemoryRepoStore:
    def __init__(self, workspaces: dict[str, Workspace]) -> None:
        self._workspaces = dict(workspaces)

    async def get_workspace(self, workspace_id):
        return self._workspaces.get(str(workspace_id))


class TestExecutionRuntimeE2E(unittest.IsolatedAsyncioTestCase):
    async def test_local_runtime_full_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            (workspace_root / "calculator.py").write_text(
                "def add(a, b):\n    return a - b\n",
                encoding="utf-8",
            )
            (workspace_root / "test_calculator.py").write_text(
                (
                    "import unittest\n\n"
                    "from calculator import add\n\n\n"
                    "class CalculatorTest(unittest.TestCase):\n"
                    "    def test_add(self):\n"
                    "        self.assertEqual(add(2, 3), 5)\n\n\n"
                    "if __name__ == '__main__':\n"
                    "    unittest.main()\n"
                ),
                encoding="utf-8",
            )

            workspace = Workspace(root_path=str(workspace_root))
            session = Session(workspace_id=workspace.workspace_id, title="runtime e2e")
            repository = SQLiteExecutionRuntimeRepository(workspace_root / "runtime.sqlite3")
            service = LocalExecutionRuntimeService(
                repository=repository,
                repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
                stream_poll_interval=0.01,
            )

            run_id = await service.enqueue_run(
                RunRequest(
                    workspace_id=workspace.workspace_id,
                    session_id=session.session_id,
                    prompt="Fix calculator and run tests",
                )
            )
            claimed = await service.claim_next_run("worker-e2e", lease_seconds=30)
            self.assertIsNotNone(claimed)
            self.assertEqual(str(claimed.run_id), run_id)
            self.assertEqual(claimed.status, RunStatus.RUNNING)

            await service.apply_patch(
                run_id,
                PatchProposal(
                    run_id=claimed.run_id,
                    task_id="task_patch_e2e",
                    summary="Fix add implementation",
                    target_paths=("calculator.py",),
                    unified_diff=(
                        "--- a/calculator.py\n"
                        "+++ b/calculator.py\n"
                        "@@ -1,2 +1,2 @@\n"
                        " def add(a, b):\n"
                        "-    return a - b\n"
                        "+    return a + b\n"
                    ),
                ),
            )

            self.assertEqual(
                (workspace_root / "calculator.py").read_text(encoding="utf-8"),
                "def add(a, b):\n    return a + b\n",
            )
            run_after_patch = await repository.get_run(run_id)
            self.assertIsNotNone(run_after_patch)
            self.assertEqual(run_after_patch.status, RunStatus.RUNNING)
            self.assertIsNone(run_after_patch.finished_at)

            command_result = await service.execute_command(
                CommandRequest(
                    run_id=claimed.run_id,
                    task_id="task_test_e2e",
                    argv=(sys.executable, "-m", "unittest", "test_calculator"),
                    cwd=".",
                    timeout_seconds=5,
                )
            )

            self.assertEqual(command_result.exit_code, 0)
            self.assertFalse(command_result.timed_out)
            run_after_command = await repository.get_run(run_id)
            self.assertIsNotNone(run_after_command)
            self.assertEqual(run_after_command.status, RunStatus.RUNNING)
            self.assertIsNone(run_after_command.finished_at)

            await service.finalize_run(
                run_id,
                RunResult(
                    run_id=claimed.run_id,
                    status=RunStatus.SUCCEEDED,
                    summary="Patched calculator and tests passed.",
                ),
            )

            final_run = await repository.get_run(run_id)
            self.assertIsNotNone(final_run)
            self.assertEqual(final_run.status, RunStatus.SUCCEEDED)
            self.assertIsNotNone(final_run.finished_at)

            events = []
            async for event in service.stream_events(run_id):
                events.append(event)

            self.assertEqual(
                [event.event_type for event in events],
                [
                    EventType.RUN_CREATED,
                    EventType.RUN_QUEUED,
                    EventType.RUN_STARTED,
                    EventType.AGENT_MESSAGE,
                    EventType.PATCH_APPLIED,
                    EventType.ARTIFACT_CREATED,
                    EventType.COMMAND_STARTED,
                    EventType.COMMAND_COMPLETED,
                    EventType.RUN_COMPLETED,
                ],
            )
            self.assertEqual(events[3].payload["kind"], "patch.started")


if __name__ == "__main__":
    unittest.main()
