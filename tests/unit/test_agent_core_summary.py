from __future__ import annotations

import unittest

from packages.shared_types import RepoContextResult, TaskStatus, new_run_id, new_workspace_id
from services.agent_core import LocalAgentCoreService
from services.agent_core.models import AgentAction, AgentActionType, AgentFailure, AgentPlan, AgentStep


class TestAgentCoreSummary(unittest.IsolatedAsyncioTestCase):
    def _make_session(self):
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Summarize the agent run deterministically",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
            ),
            current_plan=AgentPlan(
                goal="Complete the run",
                steps=[
                    AgentStep(
                        kind="inspect",
                        description="Inspect agent_core surfaces",
                        status=TaskStatus.SUCCEEDED,
                    ),
                    AgentStep(
                        kind="command",
                        description="Run agent_core tests",
                        target_files=("tests/unit/test_agent_core_plan.py",),
                        status=TaskStatus.FAILED,
                    ),
                    AgentStep(
                        kind="patch",
                        description="Update agent_core local service",
                        target_files=("services/agent_core/local.py",),
                        status=TaskStatus.PENDING,
                    ),
                ],
                summary="Run agent_core work to completion",
            ),
            action_history=[
                AgentAction(
                    type=AgentActionType.ASK_CONTEXT,
                    reason="Inspect current code",
                    requested_context=("services/agent_core/local.py",),
                ),
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run focused tests",
                    target_files=("tests/unit/test_agent_core_plan.py",),
                    command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_plan"),
                ),
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Propose local service edit",
                    target_files=("services/agent_core/local.py",),
                ),
            ],
            failure_history=[AgentFailure(stage="command", message="Targeted tests failed")],
            warnings=("tests need follow-up",),
        )
        return service, session

    async def test_summarize_run_reports_completed_failed_and_unfinished_work(self):
        # Verifies that summarize_run preserves completed steps, failures, and unfinished items.
        # This catches summaries that drop important run-state details or misreport final status.
        # The output is correct because it is derived directly from the explicit structured session state.
        service, session = self._make_session()

        summary = await service.summarize_run(session)

        self.assertEqual(summary.final_status, "failed")
        self.assertEqual(summary.completed_steps, ("Inspect agent_core surfaces",))
        self.assertEqual(
            summary.unfinished_items,
            ("Run agent_core tests", "Update agent_core local service"),
        )
        self.assertEqual(summary.failure_messages, ("Targeted tests failed",))

    async def test_summarize_run_reports_actions_files_commands_and_warnings(self):
        # Verifies that attempted actions, changed files, commands, and warnings survive summarization.
        # This catches regressions where execution history gets lost in the final report.
        # The expected fields are correct because they come from action_history and warnings on the session.
        service, session = self._make_session()

        summary = await service.summarize_run(session)

        self.assertEqual(
            summary.attempted_actions,
            ("ask_context", "run_command", "propose_patch"),
        )
        self.assertEqual(summary.changed_files, ("services/agent_core/local.py",))
        self.assertEqual(
            summary.commands_run,
            (("python", "-m", "unittest", "tests.unit.test_agent_core_plan"),),
        )
        self.assertFalse(summary.checks_passed)
        self.assertEqual(summary.warnings, ("tests need follow-up",))

    async def test_summarize_run_reports_no_command_checks_as_unknown(self):
        # Verifies that summarize_run does not invent a pass/fail check result when no command was attempted.
        # This catches summaries that overclaim test outcomes without command history.
        # The expected result is correct because checks are unknown when no run_command action exists.
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Summarize a no-command run",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
            ),
            current_plan=AgentPlan(
                goal="Summarize the work",
                steps=[
                    AgentStep(
                        kind="inspect",
                        description="Inspect files",
                        status=TaskStatus.SUCCEEDED,
                    )
                ],
            ),
            action_history=[
                AgentAction(type=AgentActionType.ASK_CONTEXT, reason="Inspect files")
            ],
            warnings=("follow up later",),
        )

        summary = await service.summarize_run(session)

        self.assertEqual(summary.final_status, "completed_with_warnings")
        self.assertIsNone(summary.checks_passed)
        self.assertEqual(summary.commands_run, ())
        self.assertEqual(summary.warnings, ("follow up later",))

    async def test_summarize_run_reports_incomplete_when_pending_steps_remain(self):
        # Verifies that pending plan steps keep the run summary in an incomplete state.
        # This catches summaries that mark unfinished runs as complete just because no failure was recorded.
        # The expected status is correct because the plan still contains work that has not succeeded yet.
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Summarize unfinished work",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
            ),
            current_plan=AgentPlan(
                goal="Finish the work",
                steps=[
                    AgentStep(
                        kind="inspect",
                        description="Inspect files",
                        status=TaskStatus.SUCCEEDED,
                    ),
                    AgentStep(
                        kind="patch",
                        description="Update local service",
                        target_files=("services/agent_core/local.py",),
                    ),
                ],
            ),
        )

        summary = await service.summarize_run(session)

        self.assertEqual(summary.final_status, "incomplete")
        self.assertEqual(summary.unfinished_items, ("Update local service",))


if __name__ == "__main__":
    unittest.main()
