from __future__ import annotations

from unittest.mock import patch
from pathlib import Path
import unittest

from packages.shared_types import RepoContextResult, new_run_id, new_workspace_id
from services.agent_core import FakeModelClient, LocalAgentCoreService
from services.agent_core.models import AgentSession
from services.agent_core.validation import validate_session_basic_shape


class TestLocalAgentCoreService(unittest.IsolatedAsyncioTestCase):
    def _make_repo_context(self, run_id, workspace_id) -> RepoContextResult:
        return RepoContextResult(
            workspace_id=workspace_id,
            run_id=run_id,
            repo_map="services/\n  agent_core/",
            warnings=("read-only in phase A-C",),
        )

    def test_create_session_returns_agent_session(self):
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()

        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Create a deterministic planning session",
            repo_context=self._make_repo_context(run_id, workspace_id),
        )

        self.assertIsInstance(session, AgentSession)
        self.assertEqual(session.run_id, run_id)
        self.assertEqual(session.workspace_id, workspace_id)
        self.assertEqual(session.user_request, "Create a deterministic planning session")
        validate_session_basic_shape(session)

    async def test_create_plan_requires_model_client(self):
        service = LocalAgentCoreService()
        session = service.create_session(
            run_id=new_run_id(),
            workspace_id=new_workspace_id(),
            user_request="Implement the next agent step",
        )

        with self.assertRaises(RuntimeError):
            await service.create_plan(session)

    async def test_other_stub_methods_are_explicitly_not_implemented(self):
        service = LocalAgentCoreService(
            model_client=FakeModelClient(
                responses=[
                    '{"goal":"Plan the work","steps":[{"kind":"inspect","description":"Inspect code"}]}'
                ]
            )
        )
        session = service.create_session(
            run_id=new_run_id(),
            workspace_id=new_workspace_id(),
            user_request="Implement the next agent step",
        )

        with self.assertRaises(NotImplementedError):
            await service.next_action(session)

        with self.assertRaises(NotImplementedError):
            await service.review_patch(
                session,
                proposed_action=None,  # type: ignore[arg-type]
            )

        with self.assertRaises(NotImplementedError):
            await service.summarize_run(session)

    async def test_create_plan_is_headless_and_uses_fake_model_client_only(self):
        fake_model = FakeModelClient(
            responses=[
                {
                    "goal": "Plan the requested work",
                    "steps": [
                        {
                            "kind": "inspect",
                            "description": "Inspect the current agent_core service surface",
                            "target_files": ["services/agent_core/service.py"],
                            "rationale": "Confirm the current API before adding more logic",
                        },
                        {
                            "kind": "complete",
                            "description": "Complete planning output",
                        },
                    ],
                }
            ]
        )
        service = LocalAgentCoreService(model_client=fake_model)
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Plan the next phase of agent_core",
            repo_context=self._make_repo_context(run_id, workspace_id),
        )

        with patch("builtins.print", side_effect=AssertionError("print should not be called")):
            plan = await service.create_plan(session)

        self.assertEqual(plan.goal, "Plan the requested work")
        self.assertEqual(plan.steps[0].kind, "inspect")
        self.assertEqual(plan.steps[0].target_files, ("services/agent_core/service.py",))
        self.assertEqual(plan.steps[1].kind, "complete")
        self.assertEqual(len(fake_model.prompts), 1)
        self.assertIn("return json only", fake_model.prompts[0].lower())

    def test_agent_core_service_stays_outside_pyclaw_import_boundary(self):
        root = Path("services/agent_core")
        self.assertTrue(root.exists())

        for path in root.rglob("*.py"):
            contents = path.read_text(encoding="utf-8")
            self.assertNotIn("import pyclaw", contents, msg=str(path))
            self.assertNotIn("from pyclaw", contents, msg=str(path))

    def test_agent_core_design_doc_exists_and_records_legacy_reuse_decision(self):
        path = Path("docs/agent_core_design.md")
        self.assertTrue(path.exists())

        contents = path.read_text(encoding="utf-8").lower()
        self.assertIn("headless", contents)
        self.assertIn("legacy coders are not reused directly", contents)
        self.assertIn("execution_runtime", contents)


if __name__ == "__main__":
    unittest.main()
