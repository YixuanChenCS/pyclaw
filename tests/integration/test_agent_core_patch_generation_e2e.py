from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from packages.shared_types import EventType, FileSummary, RepoContextResult, RunRequest, Session, Workspace
from packages.shared_types.ids import new_run_id
from services.agent_core import AgentCoreCoordinator, AgentSessionPhase, FakeModelClient, LocalAgentCoreService
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class _InMemoryRepoStore:
    def __init__(self, workspaces: dict[str, Workspace]) -> None:
        self._workspaces = dict(workspaces)

    async def get_workspace(self, workspace_id):
        return self._workspaces.get(str(workspace_id))


class TestAgentCorePatchGenerationE2E(unittest.IsolatedAsyncioTestCase):
    async def test_local_agent_core_generates_reviews_and_applies_patch_end_to_end(self):
        # Verifies the full patch chain: create_plan chooses patch work, generate_patch turns semantic edit blocks into patch_diff, review_patch accepts it, and runtime applies it.
        # This catches the live robustness gap where we previously depended on the model to hand-write a perfect unified diff.
        # The patched file and completed outcome are correct because the fake model returns one valid in-scope search/replace edit block followed by a terminal complete step.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            file_path = root / "app.txt"
            file_path.write_text("before\n", encoding="utf-8")

            workspace = Workspace(root_path=tmpdir)
            session = Session(workspace_id=workspace.workspace_id, title="agent-core-patch-e2e")
            run_id = new_run_id()
            repository = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
            runtime = LocalExecutionRuntimeService(
                repository=repository,
                repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
                stream_poll_interval=0.01,
            )
            await runtime.enqueue_run(
                RunRequest(
                    run_id=run_id,
                    workspace_id=workspace.workspace_id,
                    session_id=session.session_id,
                    prompt="Patch app.txt and finish",
                )
            )

            fake_model = FakeModelClient(
                responses=[
                    {
                        "goal": "Patch app.txt and finish",
                        "steps": [
                            {
                                "kind": "patch",
                                "description": "Patch app.txt",
                                "target_files": ["app.txt"],
                            },
                            {
                                "kind": "complete",
                                "description": "Finish the run",
                            },
                        ],
                    },
                    {
                        "path": "app.txt",
                        "search": "before\n",
                        "replace": "after\n",
                    },
                ]
            )
            agent_core = LocalAgentCoreService(model_client=fake_model, session_store=repository)
            coordinator = AgentCoreCoordinator(
                agent_core=agent_core,
                execution_runtime=runtime,
                session_store=repository,
            )

            initial_session = agent_core.create_session(
                run_id=run_id,
                workspace_id=workspace.workspace_id,
                user_request="Patch app.txt and finish",
                repo_context=RepoContextResult(
                    workspace_id=workspace.workspace_id,
                    run_id=run_id,
                    file_summaries=(
                        FileSummary(
                            path="app.txt",
                            summary="7 bytes | before",
                            content="before\n",
                        ),
                    ),
                ),
            )
            plan = await agent_core.create_plan(initial_session)
            planned_session = await repository.load_agent_session(run_id)
            if planned_session is None:
                self.fail("Expected create_plan to persist the planned session snapshot")

            outcome = await coordinator.run(
                planned_session if planned_session.current_plan is not None else initial_session
            )
            persisted_session = await repository.load_agent_session(run_id)
            events = await repository.list_events(run_id)

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(outcome.session.phase, AgentSessionPhase.COMPLETED)
            self.assertEqual(file_path.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(len(fake_model.prompts), 2)
            self.assertEqual(
                [event.event_type for event in events].count(EventType.PATCH_APPLIED),
                1,
            )
            self.assertIsNotNone(persisted_session)
            self.assertEqual(outcome.session.failure_history, [])
            self.assertFalse(
                any("status queued" in failure.message for failure in persisted_session.failure_history)
            )
            self.assertEqual(persisted_session.current_plan.goal, plan.goal)
            self.assertEqual(persisted_session.current_plan.steps[0].status.value, "succeeded")
            self.assertEqual(persisted_session.current_plan.steps[1].status.value, "succeeded")
            self.assertIn("+++ b/app.txt", persisted_session.action_history[0].patch_diff or "")
