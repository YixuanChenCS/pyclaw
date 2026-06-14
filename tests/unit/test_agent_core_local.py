from __future__ import annotations

from contextlib import ExitStack
import importlib
import os
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from packages.shared_types import FileSummary, RepoContextResult, TaskStatus, new_run_id, new_workspace_id
from services.agent_core import AgentFailure, AgentVerification, FakeModelClient, LocalAgentCoreService
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
        self.assertEqual(session.phase.value, "planning")
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

    async def test_next_patch_action_filters_read_only_reference_targets(self):
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Patch app.py using the spec",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                file_summaries=(FileSummary(path="app.py", summary="editable"),),
                reference_file_summaries=(FileSummary(path="docs/spec.md", summary="read-only"),),
            ),
            current_plan=AgentPlan(
                goal="Patch app.py",
                steps=[
                    AgentStep(
                        kind="patch",
                        description="Patch app.py",
                        target_files=("app.py", "docs/spec.md"),
                    )
                ],
            ),
        )

        action = await service.next_action(session)

        self.assertEqual(action.type, AgentActionType.PROPOSE_PATCH)
        self.assertEqual(action.target_files, ("app.py",))

    async def test_generate_patch_is_headless_and_uses_fake_model_client_only(self):
        # Verifies that patch generation stays inside the model-client boundary and does not call runtime or mutate files.
        # This catches accidental shell, filesystem, or execution-runtime side effects while generating a diff proposal.
        # The generated patch is correct because this phase should only build a prompt, parse structured edit intent, and derive a diff in memory.
        fake_model = FakeModelClient(
            responses=[
                {
                    "path": "services/agent_core/local.py",
                    "search": "def old():\n    return 'old'\n",
                    "replace": "def old():\n    return 'new'\n",
                }
            ]
        )
        service = LocalAgentCoreService(model_client=fake_model)
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Generate a patch for local.py",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/",
                file_summaries=(
                    FileSummary(
                        path="services/agent_core/local.py",
                        summary="small python file",
                        language="python",
                        content="def old():\n    return 'old'\n",
                    ),
                ),
            ),
            current_plan=AgentPlan(
                goal="Patch local.py",
                steps=[
                    AgentStep(
                        kind="patch",
                        description="Patch services/agent_core/local.py",
                        target_files=("services/agent_core/local.py",),
                    )
                ],
            ),
        )
        proposed_action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch services/agent_core/local.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("services/agent_core/local.py",),
        )

        with self._headless_side_effect_guards():
            generated = await service.generate_patch(session, proposed_action)

        self.assertIn("+++ b/services/agent_core/local.py", generated.patch_diff or "")
        self.assertEqual(generated.action_id, proposed_action.action_id)

    async def test_generate_command_is_headless_and_uses_fake_model_client_only(self):
        # Verifies that command generation stays inside the model-client boundary and does not call runtime or mutate files.
        # This catches accidental shell, filesystem, or execution-runtime side effects while generating command argv.
        # The generated command is correct because this phase should only build a prompt and parse model JSON into a command action.
        fake_model = FakeModelClient(
            responses=[
                {
                    "command_argv": ["python", "-m", "unittest", "tests.unit.test_agent_core_local"],
                    "cwd": ".",
                }
            ]
        )
        service = LocalAgentCoreService(model_client=fake_model)
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Generate a command for agent_core tests",
            repo_context=self._make_repo_context(run_id, workspace_id),
            current_plan=AgentPlan(
                goal="Run tests",
                steps=[
                    AgentStep(
                        kind="command",
                        description="Run tests for agent_core local service",
                        target_files=("tests/unit/test_agent_core_local.py",),
                    )
                ],
            ),
        )
        proposed_action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run tests for agent_core local service",
            step_id="step_1",
            action_id="action_1_run_command_step_1",
            target_files=("tests/unit/test_agent_core_local.py",),
        )

        with self._headless_side_effect_guards():
            generated = await service.generate_command(session, proposed_action)

        self.assertEqual(
            generated.command_argv,
            ("python", "-m", "unittest", "tests.unit.test_agent_core_local"),
        )
        self.assertEqual(generated.cwd, ".")

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

    def test_plan_patch_verification_skips_docs_only_patch(self):
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Patch docs only",
            repo_context=self._make_repo_context(run_id, workspace_id),
        )

        verifications = service.plan_patch_verification(
            session,
            changed_files=("README.md", "docs/agent_core_design.md"),
        )

        self.assertEqual(verifications, ())

    def test_plan_patch_verification_skips_docs_only_patch_even_with_fallback(self):
        service = LocalAgentCoreService(
            fallback_test_command=("python", "-m", "pytest", "tests/unit"),
        )
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Patch docs only",
            repo_context=self._make_repo_context(run_id, workspace_id),
        )

        verifications = service.plan_patch_verification(
            session,
            changed_files=("README.md", "docs/agent_core_design.md"),
        )

        self.assertEqual(verifications, ())

    def test_plan_patch_verification_filters_deleted_and_ignored_python_paths(self):
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Patch Python files",
            repo_context=self._make_repo_context(run_id, workspace_id),
        )

        verifications = service.plan_patch_verification(
            session,
            changed_files=(
                "app.py",
                "pkg/new_module.py",
                "old_module.py",
                ".venv/lib/site.py",
                "__pycache__/ignored.py",
                "generated/foo_pb2.py",
            ),
            deleted_files=("old_module.py",),
        )

        self.assertEqual(len(verifications), 2)
        verification = verifications[0]
        self.assertEqual(
            verification.command_argv,
            ("python", "-m", "py_compile", "app.py", "pkg/new_module.py"),
        )
        self.assertEqual(verification.changed_files, ("app.py", "pkg/new_module.py"))
        self.assertEqual(verification.verification_level, "syntax_only")
        self.assertEqual(verifications[1].verification_level, "functional_verification_missing")

    def test_plan_patch_verification_adds_targeted_pytest_for_matching_test_file(self):
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Patch runner.py",
            repo_context=self._make_repo_context(run_id, workspace_id),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tests_dir = Path(tmpdir) / "tests" / "unit"
            tests_dir.mkdir(parents=True)
            (tests_dir / "test_agent_core_runner.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

            verifications = service.plan_patch_verification(
                session,
                changed_files=("services/agent_core/runner.py",),
                workspace_root=tmpdir,
            )

        self.assertEqual(len(verifications), 2)
        self.assertEqual(
            verifications[0].command_argv,
            ("python", "-m", "py_compile", "services/agent_core/runner.py"),
        )
        self.assertEqual(
            verifications[1].command_argv,
            ("python", "-m", "pytest", "tests/unit/test_agent_core_runner.py"),
        )
        self.assertEqual(verifications[1].verification_level, "targeted_tests_passed")

    def test_plan_patch_verification_uses_fallback_when_no_targeted_test_exists(self):
        service = LocalAgentCoreService(
            fallback_test_command=("python", "-m", "pytest", "tests/unit"),
        )
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Patch helper without focused tests",
            repo_context=self._make_repo_context(run_id, workspace_id),
        )

        verifications = service.plan_patch_verification(
            session,
            changed_files=("docs/readme_helper.py",),
        )

        self.assertEqual(len(verifications), 2)
        self.assertEqual(verifications[0].verification_level, "syntax_only")
        self.assertEqual(verifications[1].kind, "fallback_pytest")
        self.assertEqual(verifications[1].verification_level, "fallback_tests_passed")
        self.assertEqual(
            verifications[1].command_argv,
            ("python", "-m", "pytest", "tests/unit"),
        )

    def test_plan_patch_verification_prefers_targeted_tests_over_fallback(self):
        service = LocalAgentCoreService(
            fallback_test_command=("python", "-m", "pytest", "tests/unit"),
        )
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Patch runner.py",
            repo_context=self._make_repo_context(run_id, workspace_id),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tests_dir = Path(tmpdir) / "tests" / "unit"
            tests_dir.mkdir(parents=True)
            (tests_dir / "test_agent_core_runner.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

            verifications = service.plan_patch_verification(
                session,
                changed_files=("services/agent_core/runner.py",),
                workspace_root=tmpdir,
            )

        self.assertEqual(len(verifications), 2)
        self.assertEqual(verifications[1].kind, "targeted_pytest")
        self.assertEqual(
            verifications[1].command_argv,
            ("python", "-m", "pytest", "tests/unit/test_agent_core_runner.py"),
        )

    async def test_next_action_proposes_patch_after_retryable_command_failure(self):
        # Verifies that retryable command failures are converted into a repair patch step instead of immediate approval escalation.
        # This catches a broken recovery loop where verification failures are recorded but agent_core does not use the new context to attempt a fix.
        # The patch targets are correct because repair prioritizes the refreshed repo context gathered around the failure.
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Fix the failing test",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                file_summaries=(
                    FileSummary(path="app.py", summary="helper definition"),
                    FileSummary(path="tests/test_app.py", summary="failing test"),
                ),
            ),
            current_plan=AgentPlan(
                goal="Fix the failure",
                steps=[
                    AgentStep(
                        step_id="step_1",
                        kind="command",
                        description="Run focused tests",
                        target_files=("tests/test_app.py",),
                    )
                ],
            ),
            action_history=[
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run focused tests",
                    step_id="step_1",
                    action_id="action_1_run_command_step_1",
                    target_files=("tests/test_app.py",),
                    command_argv=("python", "-m", "pytest", "tests/test_app.py"),
                )
            ],
            failure_history=[
                AgentFailure(
                    stage="command",
                    message="Command failed with exit code 1",
                    code="command_failed",
                    retryable=True,
                    details={"stderr": "NameError: helper_value is not defined"},
                )
            ],
        )

        with self._headless_side_effect_guards():
            action = await service.next_action(session)

        self.assertEqual(action.type, AgentActionType.PROPOSE_PATCH)
        self.assertEqual(action.target_files, ("app.py", "tests/test_app.py"))
        self.assertIn("failed verification command", action.reason)

    async def test_next_action_reruns_last_command_after_retryable_failure_following_patch(self):
        # Verifies that once a repair patch has been attempted, agent_core reruns the last failed command automatically.
        # This catches a stalled repair loop where the system fixes files but never re-verifies them.
        # Reusing the prior argv and cwd is correct because the failed verification command is the exact check that should gate completion.
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Fix the failing test",
            repo_context=self._make_repo_context(run_id, workspace_id),
            current_plan=AgentPlan(
                goal="Fix the failure",
                steps=[
                    AgentStep(
                        step_id="step_1",
                        kind="command",
                        description="Run focused tests",
                        target_files=("tests/test_app.py",),
                    )
                ],
            ),
            action_history=[
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run focused tests",
                    step_id="step_1",
                    action_id="action_1_run_command_step_1",
                    target_files=("tests/test_app.py",),
                    command_argv=("python", "-m", "pytest", "tests/test_app.py"),
                    cwd=".",
                ),
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Fix the helper usage",
                    step_id="step_2",
                    action_id="action_2_propose_patch_step_2",
                    target_files=("app.py",),
                    patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-helper_value = 0\n+helper_value = 1\n",
                ),
            ],
            failure_history=[
                AgentFailure(
                    stage="command",
                    message="Command failed with exit code 1",
                    code="command_failed",
                    retryable=True,
                )
            ],
        )

        with self._headless_side_effect_guards():
            action = await service.next_action(session)

        self.assertEqual(action.type, AgentActionType.RUN_COMMAND)
        self.assertEqual(action.command_argv, ("python", "-m", "pytest", "tests/test_app.py"))
        self.assertEqual(action.cwd, ".")
        self.assertEqual(action.target_files, ("tests/test_app.py",))

    async def test_next_action_proposes_patch_after_failed_automatic_verification(self):
        # Verifies that an automatic post-patch verification failure triggers another repair patch instead of rerunning an unrelated command.
        # This catches a bad branch where verification failures after a patch would incorrectly fall through to the generic rerun-command path.
        # The repair patch target is correct because the refreshed repo context around the failed verification should drive the next fix attempt.
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        patch_action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch app.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
            patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Patch app.py and keep it valid",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                file_summaries=(FileSummary(path="app.py", summary="python file"),),
            ),
            current_plan=AgentPlan(
                goal="Patch and verify",
                steps=[
                    AgentStep(step_id="step_1", kind="patch", description="Patch app.py", target_files=("app.py",)),
                ],
            ),
            action_history=[patch_action],
            failure_history=[
                AgentFailure(
                    stage="command",
                    message="Command failed with exit code 1",
                    code="command_failed",
                    retryable=True,
                )
            ],
            verification_history=[
                AgentVerification(
                    command_argv=("python", "-m", "py_compile", "app.py"),
                    changed_files=("app.py",),
                    stderr="SyntaxError: invalid syntax",
                    exit_code=1,
                    failure_signature="py_compile:abc123",
                    trigger_action_id=patch_action.action_id,
                )
            ],
        )

        with self._headless_side_effect_guards():
            action = await service.next_action(session)

        self.assertEqual(action.type, AgentActionType.PROPOSE_PATCH)
        self.assertEqual(action.target_files, ("app.py",))
        self.assertIn("automatic post-patch verification", action.reason)

    async def test_next_action_requests_approval_after_repeated_verification_signature(self):
        # Verifies that the agent stops retrying after the same automatic verification failure repeats too many times.
        # This catches infinite self-repair loops when repo_intelligence adds no useful new context and py_compile keeps failing the same way.
        # Approval escalation is correct because the deterministic repair strategy has exhausted its configured attempts for one signature.
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        patch_action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch app.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
            patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        repeated_signature = "py_compile:repeat123"
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Patch app.py and keep it valid",
            repo_context=self._make_repo_context(run_id, workspace_id),
            current_plan=AgentPlan(
                goal="Patch and verify",
                steps=[
                    AgentStep(step_id="step_1", kind="patch", description="Patch app.py", target_files=("app.py",)),
                ],
            ),
            action_history=[patch_action],
            failure_history=[
                AgentFailure(
                    stage="command",
                    message="Command failed with exit code 1",
                    code="command_failed",
                    retryable=True,
                )
            ],
            verification_history=[
                AgentVerification(
                    command_argv=("python", "-m", "py_compile", "app.py"),
                    changed_files=("app.py",),
                    stderr="SyntaxError: invalid syntax",
                    exit_code=1,
                    failure_signature=repeated_signature,
                    trigger_action_id=patch_action.action_id,
                ),
                AgentVerification(
                    command_argv=("python", "-m", "py_compile", "app.py"),
                    changed_files=("app.py",),
                    stderr="SyntaxError: invalid syntax",
                    exit_code=1,
                    failure_signature=repeated_signature,
                    trigger_action_id=patch_action.action_id,
                ),
                AgentVerification(
                    command_argv=("python", "-m", "py_compile", "app.py"),
                    changed_files=("app.py",),
                    stderr="SyntaxError: invalid syntax",
                    exit_code=1,
                    failure_signature=repeated_signature,
                    trigger_action_id=patch_action.action_id,
                ),
            ],
        )

        with self._headless_side_effect_guards():
            action = await service.next_action(session)

        self.assertEqual(action.type, AgentActionType.REQUEST_APPROVAL)
        self.assertIn("failed repeatedly", action.reason)

    async def test_next_action_requests_approval_after_repeated_fallback_verification_signature(self):
        # Verifies that fallback pytest failures share the same max-attempt guard as syntax verification failures.
        # This catches a loop where configured fallback tests keep failing with the same signature but never escalate.
        # Approval escalation is correct because fallback verification is part of the same deterministic automatic repair loop.
        service = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        patch_action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch helper.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("helper.py",),
            patch_diff="--- a/helper.py\n+++ b/helper.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        repeated_signature = "pytest:repeat123"
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Patch helper.py and keep fallback tests passing",
            repo_context=self._make_repo_context(run_id, workspace_id),
            current_plan=AgentPlan(
                goal="Patch and verify",
                steps=[
                    AgentStep(step_id="step_1", kind="patch", description="Patch helper.py", target_files=("helper.py",)),
                ],
            ),
            action_history=[patch_action],
            failure_history=[
                AgentFailure(
                    stage="command",
                    message="Command failed with exit code 1",
                    code="command_failed",
                    retryable=True,
                )
            ],
            verification_history=[
                AgentVerification(
                    kind="fallback_pytest",
                    verification_level="fallback_tests_failed",
                    command_argv=("python", "-m", "pytest", "tests/unit"),
                    changed_files=("helper.py",),
                    stderr="AssertionError: helper regression",
                    exit_code=1,
                    failure_signature=repeated_signature,
                    trigger_action_id=patch_action.action_id,
                ),
                AgentVerification(
                    kind="fallback_pytest",
                    verification_level="fallback_tests_failed",
                    command_argv=("python", "-m", "pytest", "tests/unit"),
                    changed_files=("helper.py",),
                    stderr="AssertionError: helper regression",
                    exit_code=1,
                    failure_signature=repeated_signature,
                    trigger_action_id=patch_action.action_id,
                ),
                AgentVerification(
                    kind="fallback_pytest",
                    verification_level="fallback_tests_failed",
                    command_argv=("python", "-m", "pytest", "tests/unit"),
                    changed_files=("helper.py",),
                    stderr="AssertionError: helper regression",
                    exit_code=1,
                    failure_signature=repeated_signature,
                    trigger_action_id=patch_action.action_id,
                ),
            ],
        )

        with self._headless_side_effect_guards():
            action = await service.next_action(session)

        self.assertEqual(action.type, AgentActionType.REQUEST_APPROVAL)
        self.assertIn("failed repeatedly", action.reason)

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
