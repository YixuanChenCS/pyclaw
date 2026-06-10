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
            iteration_count=1,
            failure_history=[failure],
            context_budget=budget,
        )

        payload = session.to_dict()
        self.assertEqual(payload["user_request"], "Implement the headless agent core skeleton")
        self.assertEqual(payload["current_plan"]["goal"], "Create a headless agent-core plan")
        self.assertEqual(payload["current_plan"]["steps"][0]["kind"], "inspect")
        self.assertEqual(payload["current_plan"]["steps"][0]["description"], "Inspect services/agent_core")
        self.assertEqual(payload["prior_artifacts"][0]["label"], "prior-summary")
        self.assertEqual(payload["failure_history"][0]["stage"], "plan")
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
        self.assertEqual(first.failure_history, [])
        self.assertIsNot(first.prior_artifacts, second.prior_artifacts)
        self.assertIsNot(first.failure_history, second.failure_history)

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
                patch_diff="--- a/file.py\n+++ b/file.py\n",
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
        self.assertEqual(actions[2].patch_diff, "--- a/file.py\n+++ b/file.py\n")
        self.assertEqual(actions[3].approval_risk_reason, "Writes outside the workspace")
        self.assertEqual(actions[4].summary_text, "Planning complete")
        self.assertEqual(actions[5].type, AgentActionType.COMPLETE)


if __name__ == "__main__":
    unittest.main()
