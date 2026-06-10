from __future__ import annotations

from contextlib import ExitStack
import importlib
import os
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
import unittest

from packages.shared_types import RepoContextResult, TaskStatus, new_run_id, new_workspace_id
from services.agent_core import FakeModelClient, LocalAgentCoreService
from services.agent_core.models import AgentAction, AgentActionType, AgentPlan, AgentStep
from services.agent_core.models import AgentSession
from services.execution_runtime.local import LocalExecutionRuntimeService
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

    def _headless_side_effect_guards(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(
            patch("builtins.print", side_effect=AssertionError("print should not be called"))
        )
        stack.enter_context(
            patch("os.system", side_effect=AssertionError("os.system should not be called"))
        )
        stack.enter_context(
            patch("subprocess.run", side_effect=AssertionError("subprocess.run should not be called"))
        )
        stack.enter_context(
            patch("pathlib.Path.write_text", side_effect=AssertionError("write_text should not be called"))
        )
        stack.enter_context(
            patch("pathlib.Path.write_bytes", side_effect=AssertionError("write_bytes should not be called"))
        )
        stack.enter_context(
            patch("pathlib.Path.unlink", side_effect=AssertionError("unlink should not be called"))
        )
        stack.enter_context(
            patch("pathlib.Path.rename", side_effect=AssertionError("rename should not be called"))
        )
        stack.enter_context(
            patch("pathlib.Path.replace", side_effect=AssertionError("replace should not be called"))
        )
        stack.enter_context(
            patch.object(
                LocalExecutionRuntimeService,
                "execute_command",
                side_effect=AssertionError("execution runtime execute_command should not be called"),
            )
        )
        stack.enter_context(
            patch.object(
                LocalExecutionRuntimeService,
                "apply_patch",
                side_effect=AssertionError("execution runtime apply_patch should not be called"),
            )
        )
        stack.enter_context(
            patch.object(
                LocalExecutionRuntimeService,
                "finalize_run",
                side_effect=AssertionError("execution runtime finalize_run should not be called"),
            )
        )
        return stack

    async def test_create_plan_is_headless_and_uses_fake_model_client_only(self):
        # Verifies that planning stays inside the model-client boundary and avoids runtime side effects.
        # This catches accidental shell, filesystem, or execution-runtime calls during prompt construction.
        # The assertions are correct because create_plan should only validate state, build a prompt, and parse model output.
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

        with self._headless_side_effect_guards():
            plan = await service.create_plan(session)

        self.assertEqual(plan.goal, "Plan the requested work")
        self.assertEqual(plan.steps[0].kind, "inspect")
        self.assertEqual(plan.steps[0].target_files, ("services/agent_core/service.py",))
        self.assertEqual(plan.steps[1].kind, "complete")
        self.assertEqual(len(fake_model.prompts), 1)
        self.assertIn("return json only", fake_model.prompts[0].lower())

    async def test_next_action_is_headless_for_inspect_step(self):
        # Verifies that LocalAgentCoreService.next_action returns a structured inspect action without side effects.
        # This catches accidental shell, mutation, or runtime-style execution in the headless decision layer.
        # Asking for the listed file is correct because the pending inspect step names that exact context.
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Inspect the agent_core service surface",
            repo_context=self._make_repo_context(run_id, workspace_id),
            current_plan=AgentPlan(
                goal="Inspect agent_core",
                steps=[
                    AgentStep(
                        kind="inspect",
                        description="Inspect services/agent_core/local.py",
                        target_files=("services/agent_core/local.py",),
                    )
                ],
            ),
        )

        with self._headless_side_effect_guards():
            action = await service.next_action(session)

        self.assertEqual(action.type, AgentActionType.ASK_CONTEXT)
        self.assertEqual(action.requested_context, ("services/agent_core/local.py",))
        self.assertEqual(action.target_files, ("services/agent_core/local.py",))

    async def test_review_patch_and_summarize_run_are_headless(self):
        # Verifies that the new Phase F methods stay purely structural and do not print or execute.
        # This catches accidental shell, filesystem, or runtime side effects leaking into the agent_core boundary.
        # The expected outputs are correct because both methods derive results only from supplied state.
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Summarize a reviewed run",
            repo_context=self._make_repo_context(run_id, workspace_id),
            current_plan=AgentPlan(
                goal="Finish the run",
                steps=[
                    AgentStep(
                        kind="patch",
                        description="Update local service",
                        target_files=("services/agent_core/local.py",),
                        status=TaskStatus.SUCCEEDED,
                    )
                ],
            ),
            action_history=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Update local service",
                    target_files=("services/agent_core/local.py",),
                )
            ],
        )
        proposed_action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Update local service",
            target_files=("services/agent_core/local.py",),
            patch_diff=(
                "--- a/services/agent_core/local.py\n"
                "+++ b/services/agent_core/local.py\n"
                "@@ -1 +1 @@\n"
                '-raise NotImplementedError("old")\n'
                '+raise NotImplementedError("new")\n'
            ),
        )

        with self._headless_side_effect_guards():
            review = await service.review_patch(session, proposed_action)
            summary = await service.summarize_run(session)

        self.assertTrue(review.accepted)
        self.assertEqual(summary.final_status, "completed")
        self.assertEqual(summary.changed_files, ("services/agent_core/local.py",))

    def test_agent_core_import_does_not_load_pyclaw_modules(self):
        # Verifies the import boundary behaviorally by checking what modules load during agent_core import.
        # This catches new hard dependencies on pyclaw even if they are hidden behind indirect imports.
        # The assertion is correct because importing services.agent_core.local should not require any pyclaw module.
        prior_agent_core = {
            name: module
            for name, module in sys.modules.items()
            if name == "services.agent_core" or name.startswith("services.agent_core.")
        }
        before_import = set(sys.modules)

        for name in list(prior_agent_core):
            sys.modules.pop(name, None)

        try:
            importlib.import_module("services.agent_core.local")
            new_modules = set(sys.modules) - before_import
        finally:
            for name in list(sys.modules):
                if name == "services.agent_core" or name.startswith("services.agent_core."):
                    sys.modules.pop(name, None)
            sys.modules.update(prior_agent_core)

        self.assertFalse(
            any(name == "pyclaw" or name.startswith("pyclaw.") for name in new_modules),
            msg=sorted(name for name in new_modules if name == "pyclaw" or name.startswith("pyclaw.")),
        )

    def test_agent_core_source_scan_for_pyclaw_imports_is_auxiliary_guardrail(self):
        # Verifies a simple source-level guardrail against direct pyclaw imports.
        # This catches obvious boundary regressions in code review, but it is not the main behavioral proof.
        # The scan is correct as an auxiliary check because agent_core source files should not contain direct pyclaw imports.
        root = Path("services/agent_core")
        self.assertTrue(root.exists())

        for path in root.rglob("*.py"):
            contents = path.read_text(encoding="utf-8")
            self.assertNotIn("import pyclaw", contents, msg=str(path))
            self.assertNotIn("from pyclaw", contents, msg=str(path))

    def test_agent_core_design_doc_exists_and_records_legacy_reuse_decision(self):
        # Verifies that the design note documenting the boundary decision still exists.
        # This catches accidental deletion of important architecture context, not runtime behavior.
        # The check is correct as documentation guardrail because the file records the intended dependency boundary.
        path = Path("docs/agent_core_design.md")
        self.assertTrue(path.exists())

        contents = path.read_text(encoding="utf-8").lower()
        self.assertIn("headless", contents)
        self.assertIn("legacy coders are not reused directly", contents)
        self.assertIn("execution_runtime", contents)


if __name__ == "__main__":
    unittest.main()
