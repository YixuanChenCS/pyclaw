from __future__ import annotations

import unittest

from packages.shared_types import RepoContextResult, TaskStatus, new_run_id, new_workspace_id
from services.agent_core import LocalAgentCoreService
from services.agent_core.models import AgentAction, AgentActionType, AgentFailure, AgentPlan, AgentStep
from services.agent_core.validation import AgentStateValidationError, MAX_AGENT_ITERATIONS


class TestAgentCoreNextAction(unittest.IsolatedAsyncioTestCase):
    def _make_session(self, *, steps, summary="Finish the run", failures=()):
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Implement deterministic next_action selection",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
            ),
            current_plan=AgentPlan(
                goal="Implement the next action",
                steps=list(steps),
                summary=summary,
            ),
            failure_history=list(failures),
        )
        return service, session

    async def test_next_action_returns_patch_proposal_for_patch_step(self):
        # Verifies that edit steps become structured patch proposals instead of repo mutations.
        # This catches accidental execution or lossy action mapping for patch work.
        # The proposed action is correct because the pending step explicitly targets files to edit.
        service, session = self._make_session(
            steps=[
                AgentStep(
                    kind="patch",
                    description="Update the next_action implementation",
                    target_files=("services/agent_core/local.py",),
                    rationale="Implement deterministic state-to-action mapping",
                )
            ]
        )

        action = await service.next_action(session)

        self.assertEqual(action.type.value, "propose_patch")
        self.assertEqual(action.action_id, "action_1_propose_patch_step_1")
        self.assertEqual(action.step_id, "step_1")
        self.assertEqual(action.reason, "Update the next_action implementation")
        self.assertEqual(action.target_files, ("services/agent_core/local.py",))
        self.assertEqual(action.summary_text, "Implement deterministic state-to-action mapping")

    async def test_next_action_returns_unittest_command_for_test_targets(self):
        # Verifies that command steps aimed at test files become deterministic unittest invocations.
        # This catches regressions where command steps lose their executable shape or wrong modules are chosen.
        # The expected command is correct because Python test-file paths map directly to unittest module names.
        service, session = self._make_session(
            steps=[
                AgentStep(
                    kind="command",
                    description="Run targeted tests for agent_core",
                    target_files=("tests/unit/test_agent_core_plan.py",),
                )
            ]
        )

        action = await service.next_action(session)

        self.assertEqual(action.type.value, "run_command")
        self.assertEqual(action.action_id, "action_1_run_command_step_1")
        self.assertEqual(
            action.command_argv,
            ("python", "-m", "unittest", "tests.unit.test_agent_core_plan"),
        )
        self.assertEqual(action.target_files, ("tests/unit/test_agent_core_plan.py",))

    async def test_next_action_returns_complete_when_no_steps_remain(self):
        # Verifies that a fully-finished plan ends in a terminal action.
        # This catches loops that would continue after every plan step has already completed.
        # The completion summary is correct because the plan summary is the canonical terminal message.
        service, session = self._make_session(
            steps=[
                AgentStep(
                    kind="inspect",
                    description="Inspect the service",
                    status=TaskStatus.SUCCEEDED,
                ),
                AgentStep(
                    kind="patch",
                    description="Update the service",
                    target_files=("services/agent_core/local.py",),
                    status=TaskStatus.SUCCEEDED,
                ),
            ],
            summary="All planned agent_core work is complete",
        )

        action = await service.next_action(session)

        self.assertEqual(action.type.value, "complete")
        self.assertEqual(action.action_id, "action_1_complete_complete")
        self.assertEqual(action.summary_text, "All planned agent_core work is complete")

    async def test_next_action_escalates_after_previous_failure(self):
        # Verifies that a recorded failure stops automatic progress and requests review.
        # This catches silent continuation after a failed action, which would hide invalid state transitions.
        # Escalation is correct because failure_history explicitly says the prior action did not succeed.
        service, session = self._make_session(
            steps=[
                AgentStep(
                    kind="patch",
                    description="Update the service",
                    target_files=("services/agent_core/local.py",),
                )
            ],
            failures=[AgentFailure(stage="command", message="Tests failed", retryable=False)],
        )

        action = await service.next_action(session)

        self.assertEqual(action.type.value, "request_approval")
        self.assertIn("command failure", action.reason)
        self.assertEqual(action.approval_message, "Tests failed")

    async def test_next_action_rejects_missing_plan(self):
        # Verifies that next_action fails loudly when no plan exists.
        # This catches callers skipping the planning phase and still trying to advance execution.
        # Raising a state-validation error is correct because there is no deterministic next step to choose.
        service = LocalAgentCoreService()
        session = service.create_session(
            run_id=new_run_id(),
            workspace_id=new_workspace_id(),
            user_request="Implement deterministic next_action selection",
        )

        with self.assertRaises(AgentStateValidationError):
            await service.next_action(session)

    async def test_next_action_rejects_patch_step_without_target_files(self):
        # Verifies that edit actions require explicit file targets.
        # This catches ambiguous patch work that would force later phases to guess where to edit.
        # Failing is correct because a structured patch action must describe its intended files.
        service, session = self._make_session(
            steps=[AgentStep(kind="patch", description="Update something", target_files=())]
        )

        with self.assertRaises(AgentStateValidationError):
            await service.next_action(session)

    async def test_next_action_rejects_running_step_state(self):
        # Verifies that next_action does not race with an already-running step.
        # This catches duplicate action issuance while the runtime is still executing prior work.
        # Failing is correct because deterministic orchestration requires no in-flight plan step here.
        service, session = self._make_session(
            steps=[
                AgentStep(
                    kind="command",
                    description="Run tests",
                    target_files=("tests/unit/test_agent_core_plan.py",),
                    status=TaskStatus.RUNNING,
                )
            ]
        )

        with self.assertRaises(AgentStateValidationError):
            await service.next_action(session)

    async def test_next_action_rejects_ambiguous_command_step(self):
        # Verifies that command steps without executable hints or known test/check targets fail loudly.
        # This catches broad fallback behavior that would invent a command and pretend the state was valid.
        # Raising is correct because the pending command step does not contain enough deterministic information.
        service, session = self._make_session(
            steps=[AgentStep(kind="command", description="Run a useful verification step")]
        )

        with self.assertRaises(AgentStateValidationError):
            await service.next_action(session)

    async def test_next_action_rejects_succeeded_step_after_pending_step(self):
        # Verifies that plan-step ordering cannot show later success after earlier pending work.
        # This catches inconsistent state transitions that would make next_action pick from a corrupted plan.
        # Rejection is correct because deterministic orchestration requires completed steps to precede pending ones.
        service, session = self._make_session(
            steps=[
                AgentStep(kind="patch", description="Pending edit", target_files=("services/agent_core/local.py",)),
                AgentStep(kind="complete", description="Already done", status=TaskStatus.SUCCEEDED),
            ]
        )

        with self.assertRaises(AgentStateValidationError):
            await service.next_action(session)

    async def test_next_action_rejects_invalid_step_status_type(self):
        # Verifies that next_action fails loudly when a step carries a non-TaskStatus value.
        # This catches broken deserialization or manual state mutation that bypasses type expectations.
        # Rejection is correct because the service cannot make deterministic decisions from invalid status values.
        service, session = self._make_session(
            steps=[
                AgentStep(  # type: ignore[arg-type]
                    kind="inspect",
                    description="Inspect file",
                    status="pending",
                )
            ]
        )

        with self.assertRaises(AgentStateValidationError):
            await service.next_action(session)

    async def test_next_action_returns_loop_guard_escalation_before_normal_work(self):
        # Verifies that loop guards are enforced through next_action instead of only via helper-level tests.
        # This catches regressions where the guard helper works but the service forgets to honor it.
        # Escalation is correct because the session already exceeds the iteration cap before choosing new work.
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Stop runaway loops",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
            ),
            current_plan=AgentPlan(
                goal="Complete the work",
                steps=[
                    AgentStep(
                        kind="patch",
                        description="Update local service",
                        target_files=("services/agent_core/local.py",),
                    )
                ],
            ),
            action_history=[
                AgentAction(type=AgentActionType.ASK_CONTEXT, reason="Inspect file"),
                AgentAction(type=AgentActionType.ASK_CONTEXT, reason="Inspect file"),
                AgentAction(type=AgentActionType.ASK_CONTEXT, reason="Inspect file"),
            ],
            iteration_count=MAX_AGENT_ITERATIONS,
        )

        action = await service.next_action(session)

        self.assertEqual(action.type, AgentActionType.REQUEST_APPROVAL)
        self.assertIn("iteration limit exceeded", action.reason.lower())
        self.assertIn("max_iterations", action.approval_risk_reason or "")


if __name__ == "__main__":
    unittest.main()
