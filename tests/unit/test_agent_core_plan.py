from __future__ import annotations

import unittest

from packages.shared_types import RepoContextResult, new_run_id, new_workspace_id
from services.agent_core import FakeModelClient, LocalAgentCoreService
from services.agent_core.validation import MAX_AGENT_PLAN_STEPS, AgentPlanValidationError


class TestAgentCorePlan(unittest.IsolatedAsyncioTestCase):
    def _make_session(self, *, responses):
        service = LocalAgentCoreService(model_client=FakeModelClient(responses=list(responses)))
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Implement create_plan with deterministic validation",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
                dependency_hints=("services.agent_core.validation",),
            ),
        )
        return service, session

    async def test_create_plan_returns_valid_agent_plan(self):
        service, session = self._make_session(
            responses=[
                """
                {
                  "goal": "Plan the create_plan implementation",
                  "steps": [
                    {
                      "kind": "inspect",
                      "description": "Inspect existing agent_core modules",
                      "target_files": ["services/agent_core/local.py"],
                      "rationale": "Understand the current headless service skeleton"
                    },
                    {
                      "type": "patch",
                      "description": "Add deterministic planning logic"
                    },
                    {
                      "kind": "complete",
                      "description": "Finish the planning phase"
                    }
                  ],
                  "summary": "High-level planning only"
                }
                """
            ]
        )

        plan = await service.create_plan(session)

        self.assertEqual(plan.goal, "Plan the create_plan implementation")
        self.assertEqual([step.kind for step in plan.steps], ["inspect", "patch", "complete"])
        self.assertEqual(plan.steps[0].target_files, ("services/agent_core/local.py",))
        self.assertEqual(plan.steps[1].description, "Add deterministic planning logic")
        self.assertEqual(plan.summary, "High-level planning only")

    async def test_create_plan_rejects_malformed_json(self):
        service, session = self._make_session(responses=['{"goal": "broken", "steps": [}'])

        with self.assertRaises(AgentPlanValidationError) as context:
            await service.create_plan(session)

        self.assertIn("malformed json", str(context.exception).lower())

    async def test_create_plan_rejects_missing_goal(self):
        service, session = self._make_session(
            responses=['{"steps":[{"kind":"inspect","description":"Inspect code"}]}']
        )

        with self.assertRaises(AgentPlanValidationError) as context:
            await service.create_plan(session)

        self.assertIn("goal", str(context.exception).lower())

    async def test_create_plan_rejects_empty_steps(self):
        service, session = self._make_session(
            responses=['{"goal":"Plan the work","steps":[]}']
        )

        with self.assertRaises(AgentPlanValidationError) as context:
            await service.create_plan(session)

        self.assertIn("steps", str(context.exception).lower())

    async def test_create_plan_rejects_unknown_step_kind(self):
        service, session = self._make_session(
            responses=[
                '{"goal":"Plan the work","steps":[{"kind":"deploy","description":"Ship it"}]}'
            ]
        )

        with self.assertRaises(AgentPlanValidationError) as context:
            await service.create_plan(session)

        self.assertIn("unsupported kind", str(context.exception).lower())

    async def test_create_plan_rejects_empty_step_description(self):
        service, session = self._make_session(
            responses=['{"goal":"Plan the work","steps":[{"kind":"inspect","description":"   "}]}']
        )

        with self.assertRaises(AgentPlanValidationError) as context:
            await service.create_plan(session)

        self.assertIn("description", str(context.exception).lower())

    async def test_create_plan_rejects_too_many_steps(self):
        steps = ",".join(
            f'{{"kind":"inspect","description":"Inspect item {index}"}}'
            for index in range(MAX_AGENT_PLAN_STEPS + 1)
        )
        service, session = self._make_session(
            responses=[f'{{"goal":"Plan the work","steps":[{steps}]}}']
        )

        with self.assertRaises(AgentPlanValidationError) as context:
            await service.create_plan(session)

        self.assertIn("may not exceed", str(context.exception).lower())

    async def test_create_plan_rejects_non_object_json_payload(self):
        # Verifies that create_plan requires a JSON object at the top level.
        # This catches permissive parsing that would accept arbitrary JSON shapes as plans.
        # Rejection is correct because the planner contract explicitly requires an object with goal and steps.
        service, session = self._make_session(responses=['["not", "a", "plan"]'])

        with self.assertRaises(AgentPlanValidationError) as context:
            await service.create_plan(session)

        self.assertIn("json object", str(context.exception).lower())

    async def test_create_plan_rejects_invalid_target_files_type(self):
        # Verifies that target_files must be a sequence of file paths, not a bare string.
        # This catches broken schema validation that would silently treat a string as iterable file names.
        # Rejection is correct because each plan step must carry normalized file paths or no targets at all.
        service, session = self._make_session(
            responses=[
                '{"goal":"Plan","steps":[{"kind":"inspect","description":"Inspect file","target_files":"services/agent_core/local.py"}]}'
            ]
        )

        with self.assertRaises(AgentPlanValidationError) as context:
            await service.create_plan(session)

        self.assertIn("target_files", str(context.exception).lower())

    async def test_create_plan_rejects_invalid_rationale_type(self):
        # Verifies that rationale must be a non-empty string when present.
        # This catches model-output coercion bugs that would accept numbers or other invalid metadata.
        # Rejection is correct because rationale is optional text, not an arbitrary JSON value.
        service, session = self._make_session(
            responses=[
                '{"goal":"Plan","steps":[{"kind":"inspect","description":"Inspect file","rationale":3}]}'
            ]
        )

        with self.assertRaises(AgentPlanValidationError) as context:
            await service.create_plan(session)

        self.assertIn("rationale", str(context.exception).lower())

    async def test_create_plan_rejects_non_mapping_model_response(self):
        # Verifies that the model client must return either a JSON string or a mapping.
        # This catches accidental adapter regressions that return unsupported response objects.
        # Rejection is correct because create_plan cannot deterministically parse arbitrary Python objects.
        service, session = self._make_session(responses=[123])

        with self.assertRaises(TypeError):
            await service.create_plan(session)


if __name__ == "__main__":
    unittest.main()
