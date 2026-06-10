from __future__ import annotations

import unittest

from packages.shared_types import new_run_id, new_workspace_id
from services.agent_core import LocalAgentCoreService
from services.agent_core.models import AgentAction, AgentActionType
from services.agent_core.validation import AgentStateValidationError


class TestAgentCoreReviewPatch(unittest.IsolatedAsyncioTestCase):
    def _make_session(self):
        service = LocalAgentCoreService()
        session = service.create_session(
            run_id=new_run_id(),
            workspace_id=new_workspace_id(),
            user_request="Review a proposed patch safely",
        )
        return service, session

    async def test_review_patch_accepts_valid_targeted_patch(self):
        # Verifies that a well-formed diff against allowed target files is accepted.
        # This catches overly strict review logic that would reject safe in-scope patches.
        # Acceptance is correct because the diff stays within the declared target file and uses valid headers.
        service, session = self._make_session()
        action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Update local service behavior",
            target_files=("services/agent_core/local.py",),
            patch_diff=(
                "--- a/services/agent_core/local.py\n"
                "+++ b/services/agent_core/local.py\n"
                "@@ -1 +1 @@\n"
                '-raise NotImplementedError("old")\n'
                '+raise NotImplementedError("new")\n'
            ),
        )

        review = await service.review_patch(session, action)

        self.assertTrue(review.accepted)
        self.assertEqual(review.changed_files, ("services/agent_core/local.py",))

    async def test_review_patch_rejects_empty_patch(self):
        # Verifies that missing patch content fails loudly instead of being treated as a no-op success.
        # This catches permissive fallback behavior that would hide malformed patch output.
        # Rejection is correct because a patch review requires concrete diff content.
        service, session = self._make_session()
        action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Empty patch",
            patch_diff="   ",
        )

        with self.assertRaises(AgentStateValidationError):
            await service.review_patch(session, action)

    async def test_review_patch_rejects_workspace_path_traversal(self):
        # Verifies that path traversal is blocked during patch review.
        # This catches unsafe diffs that would escape the workspace boundary.
        # Rejection is correct because ../ paths are not workspace-relative targets.
        service, session = self._make_session()
        action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Unsafe patch",
            patch_diff=(
                "--- a/../secret.txt\n"
                "+++ b/../secret.txt\n"
                "@@ -0,0 +1 @@\n"
                "+secret\n"
            ),
        )

        with self.assertRaises(AgentStateValidationError):
            await service.review_patch(session, action)

    async def test_review_patch_rejects_absolute_paths(self):
        # Verifies that absolute filesystem paths are blocked during review.
        # This catches diffs that try to bypass workspace-relative patching rules.
        # Rejection is correct because absolute paths can escape the controlled workspace.
        service, session = self._make_session()
        action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Absolute path patch",
            patch_diff=(
                "--- /tmp/secret.txt\n"
                "+++ /tmp/secret.txt\n"
                "@@ -0,0 +1 @@\n"
                "+secret\n"
            ),
        )

        with self.assertRaises(AgentStateValidationError):
            await service.review_patch(session, action)

    async def test_review_patch_rejects_unexpected_deleted_file(self):
        # Verifies that file deletions need explicit permission from the current action.
        # This catches destructive patch proposals being silently accepted.
        # Rejection is correct because the action did not opt into deleting any file.
        service, session = self._make_session()
        action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Delete a file",
            target_files=("services/agent_core/local.py",),
            patch_diff=(
                "--- a/services/agent_core/local.py\n"
                "+++ /dev/null\n"
                "@@ -1 +0,0 @@\n"
                '-print("x")\n'
            ),
        )

        with self.assertRaises(AgentStateValidationError):
            await service.review_patch(session, action)

    async def test_review_patch_rejects_files_outside_current_targets(self):
        # Verifies that patch review enforces the current action's target-file scope.
        # This catches patches that edit extra files the planner did not authorize.
        # Rejection is correct because the changed file is not in the action target set.
        service, session = self._make_session()
        action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Unexpected extra file",
            target_files=("services/agent_core/local.py",),
            patch_diff=(
                "--- a/services/agent_core/validation.py\n"
                "+++ b/services/agent_core/validation.py\n"
                "@@ -1 +1 @@\n"
                '-raise ValueError("old")\n'
                '+raise ValueError("new")\n'
            ),
        )

        with self.assertRaises(AgentStateValidationError):
            await service.review_patch(session, action)

    async def test_review_patch_rejects_malformed_diff_headers(self):
        # Verifies that each --- header must be paired with a following +++ header.
        # This catches malformed diff parsing that would otherwise accept incomplete patch metadata.
        # Rejection is correct because file-level patch safety depends on explicit old/new file headers.
        service, session = self._make_session()
        action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Malformed headers",
            patch_diff="--- a/services/agent_core/local.py\n@@ -1 +1 @@\n+value = 1\n",
        )

        with self.assertRaises(AgentStateValidationError):
            await service.review_patch(session, action)

    async def test_review_patch_rejects_double_dev_null_headers(self):
        # Verifies that /dev/null cannot appear on both sides of a patch header pair.
        # This catches malformed create/delete diffs that do not identify any real workspace file.
        # Rejection is correct because a patch with no concrete file path cannot be reviewed safely.
        service, session = self._make_session()
        action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Broken null patch",
            patch_diff="--- /dev/null\n+++ /dev/null\n@@ -0,0 +0,0 @@\n",
        )

        with self.assertRaises(AgentStateValidationError):
            await service.review_patch(session, action)

    async def test_review_patch_rejects_home_relative_paths(self):
        # Verifies that home-relative paths are blocked during review.
        # This catches another workspace-escape form that is unsafe even without absolute or parent-relative syntax.
        # Rejection is correct because patch targets must be explicit workspace-relative file paths.
        service, session = self._make_session()
        action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Home-relative path patch",
            patch_diff="--- a/~/.ssh/config\n+++ b/~/.ssh/config\n@@ -0,0 +1 @@\n+unsafe\n",
        )

        with self.assertRaises(AgentStateValidationError):
            await service.review_patch(session, action)

    async def test_review_patch_rejects_non_patch_action_type(self):
        # Verifies that review_patch only accepts propose_patch actions.
        # This catches callers passing unrelated action types and still getting a misleading review result.
        # Rejection is correct because only patch proposals carry the contract needed for deterministic patch review.
        service, session = self._make_session()
        action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Not a patch",
            command_argv=("python", "-m", "unittest"),
        )

        with self.assertRaises(AgentStateValidationError):
            await service.review_patch(session, action)


if __name__ == "__main__":
    unittest.main()
