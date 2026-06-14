from __future__ import annotations

import unittest

from packages.shared_types import FileSummary, RepoContextResult, new_run_id, new_workspace_id
from services.agent_core import FakeModelClient, LocalAgentCoreService
from services.agent_core.models import AgentAction, AgentActionType, AgentPlan, AgentStep
from services.agent_core.validation import AgentPatchGenerationError, AgentStateValidationError


class TestAgentCoreGeneratePatch(unittest.IsolatedAsyncioTestCase):
    def _make_session(self, *, service, content="def old():\n    return 'old'\n"):
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        return service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Generate a concrete patch for the selected step",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                file_summaries=(
                    FileSummary(
                        path="services/agent_core/local.py",
                        summary="small python file",
                        language="python",
                        content=content,
                    ),
                ),
                repo_map="services/\n  agent_core/\n",
            ),
            current_plan=AgentPlan(
                goal="Patch the local service",
                steps=[
                    AgentStep(
                        step_id="step_1",
                        kind="patch",
                        description="Patch services/agent_core/local.py",
                        target_files=("services/agent_core/local.py",),
                        rationale="Update the patch pipeline",
                    )
                ],
            ),
        )

    def _patch_action(self):
        return AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch services/agent_core/local.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("services/agent_core/local.py",),
        )

    async def test_generate_patch_builds_diff_from_direct_json_edit_intent(self):
        # Verifies the main path: structured edit intent becomes a deterministic unified diff.
        # This catches regressions where the service expects raw model diffs instead of generating them itself.
        # The diff is correct because the search block matches exactly once in the target file content.
        service = LocalAgentCoreService(
            model_client=FakeModelClient(
                responses=[
                    {
                        "path": "services/agent_core/local.py",
                        "search": "def old():\n    return 'old'\n",
                        "replace": "def old():\n    return 'new'\n",
                    }
                ]
            )
        )
        session = self._make_session(service=service)

        generated = await service.generate_patch(session, self._patch_action())

        self.assertEqual(generated.action_id, "action_1_propose_patch_step_1")
        self.assertIn("--- a/services/agent_core/local.py", generated.patch_diff or "")
        self.assertIn("+++ b/services/agent_core/local.py", generated.patch_diff or "")
        self.assertIn("+    return 'new'", generated.patch_diff or "")
        self.assertFalse(generated.allow_file_deletions)

    async def test_generate_patch_extracts_json_from_fenced_block(self):
        # Verifies that fenced ```json output is accepted when the model wraps the object in markdown.
        # This catches brittle parsing that only works for naked JSON and rejects otherwise-correct model output.
        # The generated diff is correct because the fenced payload still contains one valid structured edit intent.
        service = LocalAgentCoreService(
            model_client=FakeModelClient(
                responses=[
                    """```json
{
  "path": "services/agent_core/local.py",
  "search": "def old():\\n    return 'old'\\n",
  "replace": "def old():\\n    return 'fenced'\\n"
}
```"""
                ]
            )
        )
        session = self._make_session(service=service)

        generated = await service.generate_patch(session, self._patch_action())

        self.assertIn("+    return 'fenced'", generated.patch_diff or "")

    async def test_generate_patch_extracts_single_top_level_json_object_from_surrounding_text(self):
        # Verifies fallback extraction when the model adds prose around one JSON object.
        # This catches failures where one valid object is present but ignored because the response is not pure JSON.
        # The diff is correct because there is exactly one top-level object and it describes one exact replacement.
        service = LocalAgentCoreService(
            model_client=FakeModelClient(
                responses=[
                    (
                        "Use this patch intent:\n"
                        "{\"path\":\"services/agent_core/local.py\",\"search\":\"def old():\\n    return 'old'\\n\",\"replace\":\"def old():\\n    return 'wrapped'\\n\"}\n"
                        "End."
                    )
                ]
            )
        )
        session = self._make_session(service=service)

        generated = await service.generate_patch(session, self._patch_action())

        self.assertIn("+    return 'wrapped'", generated.patch_diff or "")

    async def test_generate_patch_rejects_multiple_top_level_json_candidates(self):
        # Verifies fail-fast behavior when the response contains more than one competing JSON object.
        # This catches permissive parsing that would arbitrarily pick one candidate and hide ambiguity.
        # Rejection is correct because patch generation must not guess which object the model intended.
        service = LocalAgentCoreService(
            model_client=FakeModelClient(
                responses=[
                    '{"path":"a.py","search":"x","replace":"y"}\n{"path":"b.py","search":"m","replace":"n"}'
                ]
            )
        )
        session = self._make_session(service=service)

        with self.assertRaises(AgentPatchGenerationError) as context:
            await service.generate_patch(session, self._patch_action())

        self.assertEqual(context.exception.failure_code, "json_parse_failed")
        self.assertIn("multiple", str(context.exception).lower())

    async def test_generate_patch_rejects_absolute_path(self):
        # Verifies that patch intent paths must stay workspace-relative.
        # This catches unsafe model output that tries to escape the workspace boundary.
        # Rejection is correct because absolute paths are outside the allowed target contract.
        service = LocalAgentCoreService(
            model_client=FakeModelClient(
                responses=[
                    {
                        "path": "/tmp/escape.py",
                        "search": "def old():\n    return 'old'\n",
                        "replace": "def old():\n    return 'new'\n",
                    }
                ]
            )
        )
        session = self._make_session(service=service)

        with self.assertRaises(AgentPatchGenerationError) as context:
            await service.generate_patch(session, self._patch_action())

        self.assertEqual(context.exception.failure_code, "schema_invalid")
        self.assertIn("workspace-relative", str(context.exception))

    async def test_generate_patch_rejects_path_outside_current_target_files(self):
        # Verifies that the structured patch intent cannot switch to an unapproved file.
        # This catches model output that edits a different path than the selected patch action allows.
        # Rejection is correct because the action explicitly scopes patching to one target file.
        service = LocalAgentCoreService(
            model_client=FakeModelClient(
                responses=[
                    {
                        "path": "other.py",
                        "search": "x",
                        "replace": "y",
                    }
                ]
            )
        )
        session = self._make_session(service=service)

        with self.assertRaises(AgentPatchGenerationError) as context:
            await service.generate_patch(session, self._patch_action())

        self.assertEqual(context.exception.failure_code, "schema_invalid")
        self.assertIn("target files", str(context.exception))

    async def test_generate_patch_rejects_search_not_found(self):
        # Verifies that missing search text fails loudly instead of producing a best-effort patch.
        # This catches permissive matching that would mutate the wrong lines when the model misses exact context.
        # Rejection is correct because the requested old text does not appear in the file at all.
        service = LocalAgentCoreService(
            model_client=FakeModelClient(
                responses=[
                    {
                        "path": "services/agent_core/local.py",
                        "search": "def missing():\n    pass\n",
                        "replace": "def missing():\n    return 1\n",
                    }
                ]
            )
        )
        session = self._make_session(service=service)

        with self.assertRaises(AgentPatchGenerationError) as context:
            await service.generate_patch(session, self._patch_action())

        self.assertEqual(context.exception.failure_code, "search_not_found")
        self.assertIn("did not match", str(context.exception).lower())

    async def test_generate_patch_rejects_ambiguous_search(self):
        # Verifies that repeated search text fails loudly instead of patching an arbitrary matching location.
        # This catches hidden nondeterminism when the model provides too little context for an exact replacement.
        # Rejection is correct because the same search block appears twice in the file content.
        service = LocalAgentCoreService(
            model_client=FakeModelClient(
                responses=[
                    {
                        "path": "services/agent_core/local.py",
                        "search": "value = 1\n",
                        "replace": "value = 2\n",
                    }
                ]
            )
        )
        session = self._make_session(
            service=service,
            content="value = 1\nprint(value)\nvalue = 1\n",
        )

        with self.assertRaises(AgentPatchGenerationError) as context:
            await service.generate_patch(session, self._patch_action())

        self.assertEqual(context.exception.failure_code, "ambiguous_search")
        self.assertIn("multiple locations", str(context.exception).lower())

    async def test_generate_patch_rejects_non_patch_action(self):
        # Verifies that only propose_patch actions can enter patch generation.
        # This catches callers routing unrelated action types through the patch generator.
        # Rejection is correct because only patch actions have target-file semantics.
        service = LocalAgentCoreService(model_client=FakeModelClient(responses=[]))
        session = self._make_session(service=service)
        action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Not a patch",
            command_argv=("python", "-m", "unittest"),
        )

        with self.assertRaises(AgentStateValidationError):
            await service.generate_patch(session, action)


if __name__ == "__main__":
    unittest.main()
