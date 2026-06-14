from __future__ import annotations

import unittest

from services.agent_core.edit_blocks import (
    SearchReplaceApplicationError,
    SearchReplaceEdit,
    apply_search_replace_edits,
    build_unified_diff,
)


class TestAgentCoreEditBlocks(unittest.TestCase):
    def test_apply_search_replace_edits_replaces_exact_block_once(self):
        # Verifies that the conservative edit-intent path replaces one exact matching block.
        # This catches regressions where a valid structured edit intent stops producing updated file text.
        # The updated text is correct because the search block appears exactly once in the source file.
        updated = apply_search_replace_edits(
            {"math_utils.py": "def add(a, b):\n    # TODO: add comment\n    return a + b\n"},
            (
                SearchReplaceEdit(
                    path="math_utils.py",
                    search="    # TODO: add comment\n",
                    replace="    # Added by agent-core live test\n",
                ),
            ),
        )

        self.assertEqual(
            updated["math_utils.py"],
            "def add(a, b):\n    # Added by agent-core live test\n    return a + b\n",
        )

    def test_apply_search_replace_edits_fails_loudly_when_search_does_not_match(self):
        # Verifies that unmatched search text is rejected instead of silently producing a partial patch.
        # This catches hidden fallback behavior that would make broken model output look successful.
        # Rejection is correct because the requested old text does not exist in the source content.
        with self.assertRaises(SearchReplaceApplicationError) as context:
            apply_search_replace_edits(
                {"math_utils.py": "def add(a, b):\n    return a + b\n"},
                (
                    SearchReplaceEdit(
                        path="math_utils.py",
                        search="    # TODO: add comment\n",
                        replace="    # Added by agent-core live test\n",
                    ),
                ),
            )

        self.assertEqual(context.exception.failure_code, "search_not_found")

    def test_apply_search_replace_edits_fails_loudly_when_search_is_ambiguous(self):
        # Verifies that repeated search text is rejected instead of patching an arbitrary occurrence.
        # This catches nondeterministic edits when the model provides too little surrounding context.
        # Rejection is correct because the same search block appears more than once in the file.
        with self.assertRaises(SearchReplaceApplicationError) as context:
            apply_search_replace_edits(
                {"math_utils.py": "value = 1\nprint(value)\nvalue = 1\n"},
                (
                    SearchReplaceEdit(
                        path="math_utils.py",
                        search="value = 1\n",
                        replace="value = 2\n",
                    ),
                ),
            )

        self.assertEqual(context.exception.failure_code, "ambiguous_search")

    def test_build_unified_diff_renders_standard_headers_from_deterministic_edit(self):
        # Verifies that deterministic search/replace still yields a normal unified diff for runtime application.
        # This catches a broken bridge where semantic edit intent succeeds in memory but no dispatchable diff is produced.
        # The diff is correct because it is generated directly from before/after file content for one target file.
        patch_diff = build_unified_diff(
            path="math_utils.py",
            before="def add(a, b):\n    # TODO: add comment\n    return a + b\n",
            after="def add(a, b):\n    # Added by agent-core live test\n    return a + b\n",
        )

        self.assertIn("--- a/math_utils.py", patch_diff)
        self.assertIn("+++ b/math_utils.py", patch_diff)
        self.assertIn("-    # TODO: add comment", patch_diff)
        self.assertIn("+    # Added by agent-core live test", patch_diff)


if __name__ == "__main__":
    unittest.main()
