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


if __name__ == "__main__":
    unittest.main()
