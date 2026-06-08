from pathlib import Path
import unittest

from services.repo_intelligence.repomap import RepoMap, get_scm_fname


class TestRepoIntelligenceRepoMap(unittest.TestCase):
    def test_queries_are_loaded_from_repo_intelligence_package(self):
        scm_path = get_scm_fname("python")

        self.assertIsNotNone(scm_path)
        self.assertTrue(Path(scm_path).exists())
        self.assertIn("services/repo_intelligence/queries", str(scm_path))

    def test_cache_dir_uses_repo_intelligence_namespace(self):
        self.assertTrue(RepoMap.TAGS_CACHE_DIR.startswith(".repo_intelligence.tags.cache"))


if __name__ == "__main__":
    unittest.main()
