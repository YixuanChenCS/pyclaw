from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from packages.shared_types import FileSummary, RepoContextResult, TaskStatus, new_run_id, new_workspace_id
from services.agent_core import AgentAction, AgentActionType, AgentContextBudget, AgentFailure, AgentPlan, AgentSessionPhase, AgentStep, AgentVerification, LocalAgentCoreService
from services.execution_runtime import SQLiteExecutionRuntimeRepository


class TestAgentSessionStore(unittest.IsolatedAsyncioTestCase):
    def _make_session(self):
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        return run_id, workspace_id, RepoContextResult(
            workspace_id=workspace_id,
            run_id=run_id,
            file_summaries=(
                FileSummary(
                    path="services/agent_core/local.py",
                    summary="small python file",
                    language="python",
                    content="def local():\n    return 'ok'\n",
                ),
            ),
            reference_file_summaries=(
                FileSummary(
                    path="docs/reference.md",
                    summary="small markdown file",
                    language="markdown",
                    content="# reference\n",
                ),
            ),
            repo_map="services/\n  agent_core/\n",
            mentioned_paths=("services/agent_core/local.py",),
            dependency_hints=("services.agent_core.local",),
            warnings=("context warning",),
        )

    async def test_repository_round_trips_canonical_agent_session(self):
        # Verifies that the runtime repository can persist and reconstruct the canonical AgentSession snapshot.
        # This catches serialization drift where nested plan, action, failure, or budget fields stop round-tripping cleanly.
        # The expected state is correct because load should return the same structured session that was just saved.
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = SQLiteExecutionRuntimeRepository(Path(tmpdir) / "runtime.sqlite3")
            run_id, workspace_id, repo_context = self._make_session()
            agent_session = LocalAgentCoreService().create_session(
                run_id=run_id,
                workspace_id=workspace_id,
                user_request="Persist the deterministic agent session",
                phase=AgentSessionPhase.AWAITING_APPROVAL,
                repo_context=repo_context,
                current_plan=AgentPlan(
                    goal="Finish agent_core work",
                    steps=[
                        AgentStep(
                            kind="patch",
                            description="Update local service",
                            step_id="step_1",
                            target_files=("services/agent_core/local.py",),
                            status=TaskStatus.SUCCEEDED,
                        )
                    ],
                    summary="Persisted summary",
                ),
                action_history=[
                    AgentAction(
                        type=AgentActionType.PROPOSE_PATCH,
                        reason="Update local service",
                        step_id="step_1",
                        action_id="action_1_propose_patch_step_1",
                        target_files=("services/agent_core/local.py",),
                    )
                ],
                pending_action=AgentAction(
                    type=AgentActionType.ASK_CONTEXT,
                    reason="Need runtime source",
                    step_id="step_2",
                    action_id="action_2_ask_context_step_2",
                    requested_context=("services/execution_runtime/local.py",),
                ),
                pending_approval_id="approval_123",
                completed_action_ids=("action_1_propose_patch_step_1",),
                failure_history=[
                    AgentFailure(
                        stage="command",
                        message="tests failed once",
                        code="command_failed",
                        retryable=True,
                        details={
                            "stdout": "F",
                            "stderr": "NameError: helper_value is not defined",
                        },
                    )
                ],
                verification_history=[
                    AgentVerification(
                        verification_level="syntax_only",
                        command_argv=("python", "-m", "py_compile", "services/agent_core/local.py"),
                        changed_files=("services/agent_core/local.py",),
                        stdout="",
                        stderr="SyntaxError: invalid syntax",
                        exit_code=1,
                        failure_signature="py_compile:deadbeef",
                        trigger_action_id="action_1_propose_patch_step_1",
                    )
                ],
                warnings=("session warning",),
                context_budget=AgentContextBudget(
                    max_input_tokens=8000,
                    remaining_input_tokens=7200,
                    max_output_tokens=2000,
                    remaining_output_tokens=1800,
                ),
            )

            await repository.save_agent_session(agent_session)
            loaded = await repository.load_agent_session(run_id)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.run_id, agent_session.run_id)
            self.assertEqual(loaded.workspace_id, agent_session.workspace_id)
            self.assertEqual(loaded.user_request, agent_session.user_request)
            self.assertEqual(loaded.phase, AgentSessionPhase.AWAITING_APPROVAL)
            self.assertEqual(loaded.repo_context.to_dict(), agent_session.repo_context.to_dict())
            self.assertEqual(loaded.current_plan.to_dict(), agent_session.current_plan.to_dict())
            self.assertEqual([action.to_dict() for action in loaded.action_history], [action.to_dict() for action in agent_session.action_history])
            self.assertEqual(loaded.pending_action.to_dict(), agent_session.pending_action.to_dict())
            self.assertEqual(loaded.pending_approval_id, agent_session.pending_approval_id)
            self.assertEqual(loaded.completed_action_ids, agent_session.completed_action_ids)
            self.assertEqual([failure.to_dict() for failure in loaded.failure_history], [failure.to_dict() for failure in agent_session.failure_history])
            self.assertEqual(
                loaded.failure_history[0].details,
                {
                    "stdout": "F",
                    "stderr": "NameError: helper_value is not defined",
                },
            )
            self.assertEqual(
                loaded.verification_history[0].command_argv,
                ("python", "-m", "py_compile", "services/agent_core/local.py"),
            )
            self.assertEqual(loaded.verification_history[0].verification_level, "syntax_only")
            self.assertEqual(loaded.verification_history[0].failure_signature, "py_compile:deadbeef")
            self.assertEqual(loaded.warnings, agent_session.warnings)
            self.assertEqual(loaded.context_budget.to_dict(), agent_session.context_budget.to_dict())

    async def test_repository_upsert_returns_latest_agent_session_snapshot(self):
        # Verifies that saving the same run twice replaces the old snapshot with the newest session state.
        # This catches stale persistence where later step-status or action-history updates are ignored.
        # The expected latest state is correct because the second save is the canonical snapshot for that run_id.
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = SQLiteExecutionRuntimeRepository(Path(tmpdir) / "runtime.sqlite3")
            run_id, workspace_id, repo_context = self._make_session()
            service = LocalAgentCoreService()
            session = service.create_session(
                run_id=run_id,
                workspace_id=workspace_id,
                user_request="Persist updated agent session",
                phase=AgentSessionPhase.READY,
                repo_context=repo_context,
                current_plan=AgentPlan(
                    goal="Finish agent_core work",
                    steps=[
                        AgentStep(
                            kind="command",
                            description="Run focused tests",
                            step_id="step_1",
                            target_files=("tests/unit/test_agent_core_runner.py",),
                        )
                    ],
                ),
            )

            await repository.save_agent_session(session)

            updated_plan = replace(
                session.current_plan,
                steps=[replace(session.current_plan.steps[0], status=TaskStatus.SUCCEEDED)],
            )
            updated_session = replace(
                session,
                current_plan=updated_plan,
                action_history=[
                    AgentAction(
                        type=AgentActionType.RUN_COMMAND,
                        reason="Run focused tests",
                        step_id="step_1",
                        action_id="action_1_run_command_step_1",
                        command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
                    )
                ],
                pending_action=AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Finish the run",
                    step_id="step_2",
                    action_id="action_2_complete_step_2",
                    summary_text="Everything succeeded",
                ),
                completed_action_ids=["action_1_run_command_step_1"],
                phase=AgentSessionPhase.EXECUTING,
            )

            await repository.save_agent_session(updated_session)
            loaded = await repository.load_agent_session(run_id)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.current_plan.steps[0].status, TaskStatus.SUCCEEDED)
            self.assertEqual(loaded.action_history[0].step_id, "step_1")
            self.assertEqual(
                loaded.action_history[0].command_argv,
                ("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
            )
            self.assertEqual(loaded.phase, AgentSessionPhase.EXECUTING)
            self.assertEqual(loaded.pending_action.action_id, "action_2_complete_step_2")
            self.assertEqual(loaded.completed_action_ids, ["action_1_run_command_step_1"])


if __name__ == "__main__":
    unittest.main()
