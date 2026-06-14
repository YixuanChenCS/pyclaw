from __future__ import annotations

import unittest

from packages.shared_types import RepoContextResult, new_run_id, new_workspace_id
from services.agent_core import FakeModelClient, LocalAgentCoreService
from services.agent_core.models import AgentAction, AgentActionType, AgentPlan, AgentStep
from services.agent_core.validation import AgentCommandValidationError, AgentStateValidationError


class TestAgentCoreGenerateCommand(unittest.IsolatedAsyncioTestCase):
    def _make_session(self, *, service):
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        return service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Generate a concrete command for the selected step",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
            ),
            current_plan=AgentPlan(
                goal="Run targeted verification",
                steps=[
                    AgentStep(
                        step_id="step_1",
                        kind="command",
                        description="Run tests for agent_core",
                        target_files=("tests/unit/test_agent_core_plan.py",),
                        rationale="Validate the planner path",
                    )
                ],
            ),
        )

    def _command_action(self):
        return AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run tests for agent_core",
            step_id="step_1",
            action_id="action_1_run_command_step_1",
            target_files=("tests/unit/test_agent_core_plan.py",),
        )

    async def test_generate_command_returns_enriched_command_action(self):
        # Verifies that generate_command turns a command intent into a concrete argv payload.
        # This catches the broken command chain where next_action selected command work but no executable argv was produced.
        # The enriched action is correct because it preserves the selected command identity and adds the model-produced argv.
        fake_model = FakeModelClient(
            responses=[
                {
                    "command_argv": ["python", "-m", "unittest", "tests.unit.test_agent_core_plan"],
                    "cwd": ".",
                }
            ]
        )
        service = LocalAgentCoreService(model_client=fake_model)
        session = self._make_session(service=service)

        generated = await service.generate_command(session, self._command_action())

        self.assertEqual(generated.action_id, "action_1_run_command_step_1")
        self.assertEqual(
            generated.command_argv,
            ("python", "-m", "unittest", "tests.unit.test_agent_core_plan"),
        )
        self.assertEqual(generated.cwd, ".")
        self.assertEqual(len(fake_model.prompts), 1)
        self.assertIn("return json only", fake_model.prompts[0].lower())

    async def test_generate_command_rejects_non_command_action(self):
        # Verifies that only run_command actions can enter the command-generation phase.
        # This catches callers accidentally routing patch or approval actions through command generation.
        # Rejection is correct because only command actions carry the contract needed for argv synthesis.
        service = LocalAgentCoreService(model_client=FakeModelClient(responses=[]))
        session = self._make_session(service=service)
        action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Not a command",
            target_files=("services/agent_core/local.py",),
        )

        with self.assertRaises(AgentStateValidationError):
            await service.generate_command(session, action)

    async def test_generate_command_requires_model_client(self):
        # Verifies that command generation uses the model-client boundary instead of inventing argv locally.
        # This catches permissive fallback behavior that would pretend a command was generated without a configured model client.
        # Rejection is correct because this phase explicitly depends on model output to produce command_argv.
        service = LocalAgentCoreService()
        session = self._make_session(service=service)

        with self.assertRaises(RuntimeError):
            await service.generate_command(session, self._command_action())

    async def test_generate_command_rejects_malformed_json(self):
        # Verifies that malformed model JSON fails loudly instead of being treated as a partial command response.
        # This catches weak parsing that would swallow broken model output and continue with invalid state.
        # Rejection is correct because command generation requires a valid JSON object with command_argv.
        service = LocalAgentCoreService(model_client=FakeModelClient(responses=['{"command_argv": [}']))
        session = self._make_session(service=service)

        with self.assertRaises(AgentCommandValidationError) as context:
            await service.generate_command(session, self._command_action())

        self.assertIn("malformed", str(context.exception).lower())

    async def test_generate_command_rejects_missing_command_argv(self):
        # Verifies that command generation requires a concrete argv list and does not accept commandless responses.
        # This catches the exact bug where command steps could advance without producing command_argv.
        # Rejection is correct because RUN_COMMAND dispatch requires a non-empty argv payload.
        service = LocalAgentCoreService(model_client=FakeModelClient(responses=['{"cwd": "."}']))
        session = self._make_session(service=service)

        with self.assertRaises(AgentCommandValidationError) as context:
            await service.generate_command(session, self._command_action())

        self.assertIn("command_argv", str(context.exception).lower())

    async def test_generate_command_rejects_invalid_cwd_type(self):
        # Verifies that cwd must stay a string when present rather than arbitrary JSON.
        # This catches model outputs that would blur execution context into an untyped field.
        # Rejection is correct because downstream runtime dispatch depends on cwd being a real path string or null.
        service = LocalAgentCoreService(
            model_client=FakeModelClient(
                responses=[
                    {
                        "command_argv": ["python", "-m", "unittest"],
                        "cwd": 3,
                    }
                ]
            )
        )
        session = self._make_session(service=service)

        with self.assertRaises(AgentCommandValidationError) as context:
            await service.generate_command(session, self._command_action())

        self.assertIn("cwd", str(context.exception).lower())
