from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from packages.shared_types import EventType, RunRequest, Session, Workspace
from packages.shared_types.ids import new_run_id
from services.agent_core import AgentCoreCoordinator, AgentSessionPhase, FakeModelClient, LocalAgentCoreService
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class _InMemoryRepoStore:
    def __init__(self, workspaces: dict[str, Workspace]) -> None:
        self._workspaces = dict(workspaces)

    async def get_workspace(self, workspace_id):
        return self._workspaces.get(str(workspace_id))


class TestAgentCoreCommandGenerationE2E(unittest.IsolatedAsyncioTestCase):
    async def test_local_agent_core_generates_and_executes_command_end_to_end(self):
        # Verifies the full command chain: create_plan chooses command work, generate_command produces argv, and runtime executes it before completion.
        # This catches the previous gap where command steps either guessed argv too early or reached dispatch without a concrete payload.
        # The completed outcome is correct because the fake model returns a valid command payload followed by a terminal complete step.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            session = Session(workspace_id=workspace.workspace_id, title="agent-core-command-e2e")
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
                    prompt="Run a focused unittest and finish",
                )
            )
            await runtime.claim_next_run("worker-e2e", lease_seconds=30)

            fake_model = FakeModelClient(
                responses=[
                    {
                        "goal": "Run a focused unittest and finish",
                        "steps": [
                            {
                                "kind": "command",
                                "description": "Run a verification command",
                            },
                            {
                                "kind": "complete",
                                "description": "Finish the run",
                            },
                        ],
                    },
                    {
                        "command_argv": [
                            sys.executable,
                            "-c",
                            "print('agent-core-command-e2e')",
                        ],
                        "cwd": ".",
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
                user_request="Run a focused unittest and finish",
            )
            await agent_core.create_plan(initial_session)
            planned_session = await repository.load_agent_session(run_id)
            if planned_session is None:
                self.fail("Expected create_plan to persist the planned session snapshot")

            outcome = await coordinator.run(planned_session)
            persisted_session = await repository.load_agent_session(run_id)
            events = await repository.list_events(run_id)

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(outcome.session.phase, AgentSessionPhase.COMPLETED)
            self.assertEqual(len(fake_model.prompts), 2)
            self.assertEqual(
                [event.event_type for event in events].count(EventType.COMMAND_STARTED),
                1,
            )
            self.assertEqual(
                [event.event_type for event in events].count(EventType.COMMAND_COMPLETED),
                1,
            )
            self.assertIsNotNone(persisted_session)
            self.assertEqual(
                persisted_session.action_history[0].command_argv,
                (sys.executable, "-c", "print('agent-core-command-e2e')"),
            )
            self.assertEqual(persisted_session.current_plan.steps[0].status.value, "succeeded")
            self.assertEqual(persisted_session.current_plan.steps[1].status.value, "succeeded")
