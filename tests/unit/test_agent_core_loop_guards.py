from __future__ import annotations

import unittest

from packages.shared_types import TaskStatus, new_run_id, new_workspace_id
from services.agent_core.models import (
    AgentAction,
    AgentActionType,
    AgentContextBudget,
    AgentFailure,
    AgentPlan,
    AgentSession,
    AgentStep,
)
from services.agent_core.validation import (
    MAX_AGENT_ITERATIONS,
    MAX_NO_PROGRESS_ITERATIONS,
    evaluate_loop_guard,
)


class TestAgentCoreLoopGuards(unittest.TestCase):
    def _make_session(self, **overrides):
        defaults = {
            "run_id": new_run_id(),
            "workspace_id": new_workspace_id(),
            "user_request": "Protect the loop deterministically",
            "current_plan": AgentPlan(
                goal="Complete the work",
                steps=[
                    AgentStep(
                        kind="patch",
                        description="Update local service",
                        target_files=("services/agent_core/local.py",),
                    )
                ],
            ),
            "action_history": [],
            "iteration_count": 0,
            "failure_history": [],
            "warnings": [],
            "context_budget": None,
        }
        defaults.update(overrides)
        return AgentSession(**defaults)

    def test_loop_guard_triggers_on_max_iterations(self):
        # Verifies that runaway iteration counts stop the loop before more work is issued.
        # This catches endless retries that would otherwise continue without a hard cap.
        # Triggering is correct because the session iteration count already exceeds the configured limit.
        session = self._make_session(iteration_count=MAX_AGENT_ITERATIONS)

        result = evaluate_loop_guard(session)

        self.assertTrue(result.triggered)
        self.assertEqual(result.guard_kind, "max_iterations")

    def test_loop_guard_triggers_on_repeated_same_action(self):
        # Verifies that replaying the same action shape too many times is blocked.
        # This catches loops that keep proposing identical work without making progress.
        # Triggering is correct because the recent action history is exactly repetitive.
        repeated_action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run focused tests",
            command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_plan"),
        )
        session = self._make_session(
            action_history=[repeated_action, repeated_action, repeated_action],
            iteration_count=2,
        )

        result = evaluate_loop_guard(session)

        self.assertTrue(result.triggered)
        self.assertEqual(result.guard_kind, "repeated_action")

    def test_loop_guard_triggers_on_repeated_context_requests(self):
        # Verifies that repeated ASK_CONTEXT turns are capped even when the requested files differ.
        # This catches the loop where the agent keeps asking for more context instead of incorporating it.
        # Triggering is correct because three consecutive context requests indicate stalled context acquisition.
        session = self._make_session(
            action_history=[
                AgentAction(
                    type=AgentActionType.ASK_CONTEXT,
                    reason="Inspect service",
                    requested_context=("services/agent_core/local.py",),
                ),
                AgentAction(
                    type=AgentActionType.ASK_CONTEXT,
                    reason="Inspect tests",
                    requested_context=("tests/unit/test_agent_core_runner.py",),
                ),
                AgentAction(
                    type=AgentActionType.ASK_CONTEXT,
                    reason="Inspect runtime",
                    requested_context=("services/execution_runtime/local.py",),
                ),
            ],
            iteration_count=2,
        )

        result = evaluate_loop_guard(session)

        self.assertTrue(result.triggered)
        self.assertEqual(result.guard_kind, "repeated_context_requests")

    def test_loop_guard_triggers_on_repeated_patch_review_failures(self):
        # Verifies that consecutive review failures stop the loop instead of allowing repeated bad patches.
        # This catches retry storms where invalid patch proposals keep being recycled.
        # Triggering is correct because the latest failure streak is entirely patch-review failures.
        session = self._make_session(
            failure_history=[
                AgentFailure(stage="review_patch", message="bad patch 1"),
                AgentFailure(stage="review_patch", message="bad patch 2"),
            ]
        )

        result = evaluate_loop_guard(session)

        self.assertTrue(result.triggered)
        self.assertEqual(result.guard_kind, "repeated_patch_review_failures")

    def test_loop_guard_triggers_on_repeated_command_failures(self):
        # Verifies that repeated command failures stop automatic retries.
        # This catches repeated failing test runs that would otherwise continue unchanged.
        # Triggering is correct because command failures are consecutive at the end of failure history.
        session = self._make_session(
            failure_history=[
                AgentFailure(stage="command", message="tests failed 1"),
                AgentFailure(stage="command", message="tests failed 2"),
            ]
        )

        result = evaluate_loop_guard(session)

        self.assertTrue(result.triggered)
        self.assertEqual(result.guard_kind, "repeated_command_failures")

    def test_loop_guard_triggers_on_no_progress(self):
        # Verifies that several iterations with no completed steps are treated as a stalled loop.
        # This catches agents that keep cycling through attempts without advancing plan state.
        # Triggering is correct because the session has repeated iterations and zero succeeded steps.
        session = self._make_session(
            iteration_count=MAX_NO_PROGRESS_ITERATIONS,
            action_history=[
                AgentAction(type=AgentActionType.ASK_CONTEXT, reason="Inspect file"),
                AgentAction(type=AgentActionType.ASK_CONTEXT, reason="Inspect file again"),
                AgentAction(type=AgentActionType.REQUEST_APPROVAL, reason="Need more help"),
            ],
        )

        result = evaluate_loop_guard(session)

        self.assertTrue(result.triggered)
        self.assertEqual(result.guard_kind, "no_progress")

    def test_loop_guard_triggers_on_context_budget_exhaustion(self):
        # Verifies that exhausted context budget stops the loop deterministically.
        # This catches continued planning after the tracked token budget has been consumed.
        # Triggering is correct because remaining input tokens are already depleted.
        session = self._make_session(
            context_budget=AgentContextBudget(remaining_input_tokens=0),
        )

        result = evaluate_loop_guard(session)

        self.assertTrue(result.triggered)
        self.assertEqual(result.guard_kind, "context_overflow")

    def test_loop_guard_allows_normal_progress(self):
        # Verifies that ordinary progress does not falsely trigger any guard.
        # This catches over-aggressive loop protection that would stop healthy runs.
        # No guard is correct because the plan has a succeeded step and no repeated failure pattern.
        session = self._make_session(
            current_plan=AgentPlan(
                goal="Complete the work",
                steps=[
                    AgentStep(
                        kind="inspect",
                        description="Inspect local service",
                        status=TaskStatus.SUCCEEDED,
                    ),
                    AgentStep(
                        kind="patch",
                        description="Update local service",
                        target_files=("services/agent_core/local.py",),
                    ),
                ],
            ),
            action_history=[
                AgentAction(type=AgentActionType.ASK_CONTEXT, reason="Inspect file"),
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Prepare patch",
                    target_files=("services/agent_core/local.py",),
                ),
            ],
            iteration_count=2,
        )

        result = evaluate_loop_guard(session)

        self.assertFalse(result.triggered)
        self.assertIsNone(result.guard_kind)

    def test_loop_guard_uses_max_iterations_before_other_matching_guards(self):
        # Verifies deterministic guard priority when multiple runaway conditions are present.
        # This catches unstable behavior where different guards could trigger depending on implementation order.
        # Max-iteration precedence is correct because it is the first hard stop in the configured guard sequence.
        repeated_action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run focused tests",
            command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_plan"),
        )
        session = self._make_session(
            iteration_count=MAX_AGENT_ITERATIONS,
            action_history=[repeated_action, repeated_action, repeated_action],
            failure_history=[
                AgentFailure(stage="command", message="tests failed 1"),
                AgentFailure(stage="command", message="tests failed 2"),
            ],
        )

        result = evaluate_loop_guard(session)

        self.assertTrue(result.triggered)
        self.assertEqual(result.guard_kind, "max_iterations")


if __name__ == "__main__":
    unittest.main()
