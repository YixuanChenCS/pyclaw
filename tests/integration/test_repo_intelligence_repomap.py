from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

from services.repo_intelligence.repomap import RepoMap, get_scm_fname


class _FakeModel:
    def token_count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)


class _FakeIO:
    def __init__(self) -> None:
        self.outputs: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def read_text(self, fname: str) -> str:
        return Path(fname).read_text(encoding="utf-8", errors="replace")

    def tool_output(self, message: str = "") -> None:
        if message:
            self.outputs.append(message)

    def tool_warning(self, message: str = "") -> None:
        if message:
            self.warnings.append(message)

    def tool_error(self, message: str = "") -> None:
        if message:
            self.errors.append(message)


class TestRepoIntelligenceRepoMapIntegration(unittest.TestCase):
    def test_repomap_uses_repo_intelligence_queries_and_cache_namespace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_map = RepoMap(main_model=_FakeModel(), root=tmpdir, io=_FakeIO())

            self.assertTrue(RepoMap.TAGS_CACHE_DIR.startswith(".repo_intelligence.tags.cache"))
            self.assertTrue(Path(get_scm_fname("python")).exists())
            self.assertIn("services/repo_intelligence/queries", str(get_scm_fname("python")))
            self.assertTrue(
                isinstance(repo_map.TAGS_CACHE, dict) or hasattr(repo_map.TAGS_CACHE, "close")
            )
            repo_map.close()

    def test_repomap_builds_map_for_python_files_when_optional_deps_exist(self):
        required = ("networkx", "grep_ast", "tree_sitter", "pygments")
        missing = [name for name in required if importlib.util.find_spec(name) is None]
        if missing:
            self.skipTest(f"optional repomap deps unavailable: {', '.join(missing)}")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "app.py"
            helper = root / "helper.py"
            readme = root / "README.md"
            app.write_text("from helper import helper_fn\n\ndef app_fn():\n    return helper_fn()\n", encoding="utf-8")
            helper.write_text("def helper_fn():\n    return 1\n", encoding="utf-8")
            readme.write_text("# demo\n", encoding="utf-8")

            repo_map = RepoMap(main_model=_FakeModel(), root=tmpdir, io=_FakeIO(), refresh="files")
            result = repo_map.get_repo_map([], [str(app), str(helper), str(readme)])

            self.assertIsNotNone(result)
            self.assertIn("app.py", result)
            self.assertIn("helper.py", result)
            self.assertIn("README.md", result)
            self.assertIn("helper_fn", result)
            repo_map.close()


if __name__ == "__main__":
    unittest.main()
