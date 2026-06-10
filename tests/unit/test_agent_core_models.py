from __future__ import annotations

import unittest

from packages.shared_types import ArtifactRef, RepoContextResult, TaskStatus, new_run_id, new_workspace_id
from services.agent_core.models import (
    AgentAction,
    AgentActionType,
    AgentContextBudget,
    AgentFailure,
    AgentPlan,
    AgentSession,
    AgentStep,
    LoopGuardResult,
    PatchReview,
    RunSummary,
)


class TestAgentCoreModels(unittest.TestCase):
    def _make_repo_context(self, run_id, workspace_id) -> RepoContextResult:
        return RepoContextResult(
            workspace_id=workspace_id,
            run_id=run_id,
            repo_map="pkg/\n  service.py",
            dependency_hints=("services.agent_core.local",),
        )

    def test_agent_session_construction_keeps_plain_serializable_state(self):
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        repo_context = self._make_repo_context(run_id, workspace_id)
        plan = AgentPlan(
            goal="Create a headless agent-core plan",
            steps=[
                AgentStep(
                    kind="inspect",
                    description="Inspect services/agent_core",
                    target_files=("services/agent_core/local.py",),
                    rationale="Understand the current local service boundary",
                    status=TaskStatus.PENDING,
                )
            ],
            summary="Build headless agent core foundation",
        )
        failure = AgentFailure(stage="plan", message="Not implemented yet", retryable=True)
        artifact = ArtifactRef(
            artifact_id="artifact_summary",
            run_id=run_id,
            artifact_type="summary",
            label="prior-summary",
            uri="memory://summary",
        )
        budget = AgentContextBudget(max_input_tokens=8000, remaining_input_tokens=6000)

        session = AgentSession(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Implement the headless agent core skeleton",
            repo_context=repo_context,
            current_plan=plan,
            prior_artifacts=[artifact],
            action_history=[
                AgentAction(
                    type=AgentActionType.ASK_CONTEXT,
                    reason="Need local service context",
                    requested_context=("services/agent_core/local.py",),
                )
            ],
            iteration_count=1,
            failure_history=[failure],
            warnings=["summary warning"],
            context_budget=budget,
        )

        payload = session.to_dict()
        self.assertEqual(payload["user_request"], "Implement the headless agent core skeleton")
        self.assertEqual(payload["current_plan"]["goal"], "Create a headless agent-core plan")
        self.assertEqual(payload["current_plan"]["steps"][0]["kind"], "inspect")
        self.assertEqual(payload["current_plan"]["steps"][0]["description"], "Inspect services/agent_core")
        self.assertEqual(payload["prior_artifacts"][0]["label"], "prior-summary")
        self.assertEqual(payload["action_history"][0]["type"], "ask_context")
        self.assertEqual(payload["failure_history"][0]["stage"], "plan")
        self.assertEqual(payload["warnings"][0], "summary warning")
        self.assertEqual(payload["context_budget"]["remaining_input_tokens"], 6000)

    def test_agent_session_uses_safe_mutable_defaults(self):
        run_id = new_run_id()
        workspace_id = new_workspace_id()

        first = AgentSession(run_id=run_id, workspace_id=workspace_id, user_request="first")
        second = AgentSession(
            run_id=new_run_id(),
            workspace_id=new_workspace_id(),
            user_request="second",
        )

        self.assertEqual(first.prior_artifacts, [])
        self.assertEqual(first.action_history, [])
        self.assertEqual(first.failure_history, [])
        self.assertEqual(first.warnings, [])
        self.assertIsNot(first.prior_artifacts, second.prior_artifacts)
        self.assertIsNot(first.action_history, second.action_history)
        self.assertIsNot(first.failure_history, second.failure_history)
        self.assertIsNot(first.warnings, second.warnings)

    def test_agent_action_type_contains_exact_expected_values(self):
        self.assertEqual(
            {action_type.value for action_type in AgentActionType},
            {
                "ask_context",
                "run_command",
                "propose_patch",
                "request_approval",
                "summarize",
                "complete",
            },
        )

    def test_agent_action_can_represent_each_allowed_action_type(self):
        actions = [
            AgentAction(
                type=AgentActionType.ASK_CONTEXT,
                reason="Need additional repository files",
                requested_context=("services/agent_core/local.py",),
            ),
            AgentAction(
                type=AgentActionType.RUN_COMMAND,
                reason="Future runtime command request",
                command_argv=("pytest", "-q"),
                cwd=".",
            ),
            AgentAction(
                type=AgentActionType.PROPOSE_PATCH,
                reason="Future patch proposal",
                target_files=("services/agent_core/local.py",),
                patch_diff="--- a/file.py\n+++ b/file.py\n",
                allow_file_deletions=False,
            ),
            AgentAction(
                type=AgentActionType.REQUEST_APPROVAL,
                reason="Human approval required",
                approval_message="Run an unsafe command",
                approval_risk_reason="Writes outside the workspace",
            ),
            AgentAction(
                type=AgentActionType.SUMMARIZE,
                reason="Summarize progress",
                summary_text="Planning complete",
            ),
            AgentAction(
                type=AgentActionType.COMPLETE,
                reason="Run is complete",
                summary_text="All steps finished",
            ),
        ]

        self.assertEqual([action.type for action in actions][0], AgentActionType.ASK_CONTEXT)
        self.assertEqual(actions[1].command_argv, ("pytest", "-q"))
        self.assertEqual(actions[2].target_files, ("services/agent_core/local.py",))
        self.assertEqual(actions[2].patch_diff, "--- a/file.py\n+++ b/file.py\n")
        self.assertEqual(actions[3].approval_risk_reason, "Writes outside the workspace")
        self.assertEqual(actions[4].summary_text, "Planning complete")
        self.assertEqual(actions[5].type, AgentActionType.COMPLETE)

    def test_phase_f_models_serialize_cleanly(self):
        review = PatchReview(
            accepted=True,
            reason="Patch review passed",
            changed_files=("services/agent_core/local.py",),
            patch_diff="--- a/x\n+++ b/x\n",
        )
        summary = RunSummary(
            final_status="completed",
            completed_steps=("Inspect files",),
            attempted_actions=("ask_context", "propose_patch"),
            changed_files=("services/agent_core/local.py",),
            commands_run=(("python", "-m", "unittest"),),
            checks_passed=True,
            warnings=("none",),
        )
        guard = LoopGuardResult(triggered=True, guard_kind="max_iterations", reason="stopped")

        self.assertTrue(review.to_dict()["accepted"])
        self.assertEqual(summary.to_dict()["final_status"], "completed")
        self.assertEqual(guard.to_dict()["guard_kind"], "max_iterations")


if __name__ == "__main__":
    unittest.main()
