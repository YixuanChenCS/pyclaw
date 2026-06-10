from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from packages.shared_types import (
    CommandRequest,
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


async def main() -> None:
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
        session = Session(workspace_id=workspace.workspace_id, title="runtime demo")
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
        claimed = await service.claim_next_run("demo-worker", lease_seconds=30)
        if claimed is None:
            raise RuntimeError("No run was claimed")

        await service.apply_patch(
            run_id,
            PatchProposal(
                run_id=claimed.run_id,
                task_id="task_patch_demo",
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

        result = await service.execute_command(
            CommandRequest(
                run_id=claimed.run_id,
                task_id="task_test_demo",
                argv=(sys.executable, "-m", "unittest", "test_calculator"),
                cwd=".",
                timeout_seconds=5,
            )
        )
        if result.exit_code != 0 or result.timed_out:
            raise RuntimeError(
                f"Demo command failed: exit_code={result.exit_code} timed_out={result.timed_out}"
            )

        await service.finalize_run(
            run_id,
            RunResult(
                run_id=claimed.run_id,
                status=RunStatus.SUCCEEDED,
                summary="Demo completed successfully.",
            ),
        )

        event_types = []
        async for event in service.stream_events(run_id):
            event_types.append(event.event_type.value)

        print(f"Workspace: {workspace_root}")
        print(f"Run: {run_id}")
        print("Replayed events:")
        for event_type in event_types:
            print(f"- {event_type}")


if __name__ == "__main__":
    asyncio.run(main())
