from __future__ import annotations

import importlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from packages.shared_types import ErrorCode, ErrorCodeContractError, RepoContextRequest, Workspace
from packages.shared_types.ids import new_run_id
from services.repo_intelligence import RepoIntelligenceService
from services.repo_intelligence.local import LocalRepoIntelligenceService


class TestLocalRepoIntelligenceService(unittest.IsolatedAsyncioTestCase):
    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Test User"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@example.com"],
            check=True,
            capture_output=True,
            text=True,
        )

    def _commit_all(self, root: Path, message: str) -> str:
        subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", message],
            check=True,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    async def test_service_instantiates(self):
        service = LocalRepoIntelligenceService()
        self.assertIsInstance(service, LocalRepoIntelligenceService)

    async def test_service_satisfies_interface(self):
        service = LocalRepoIntelligenceService()
        self.assertIsInstance(service, RepoIntelligenceService)

    async def test_inspect_workspace_on_git_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._init_git_repo(root)
            src_dir = root / "src"
            src_dir.mkdir()
            (root / "README.md").write_text("# demo\n", encoding="utf-8")
            (src_dir / "app.py").write_text("print('hello')\n", encoding="utf-8")
            commit_sha = self._commit_all(root, "initial import")

            service = LocalRepoIntelligenceService()
            workspace = Workspace(root_path=str(src_dir))
            inspected = await service.inspect_workspace(workspace)

            self.assertEqual(inspected.root_path, str(root.resolve()))
            self.assertEqual(inspected.commit_sha, commit_sha)
            self.assertTrue(inspected.branch)

    async def test_inspect_workspace_handles_non_git_directory_gracefully(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "notes.txt").write_text("plain\n", encoding="utf-8")

            service = LocalRepoIntelligenceService()
            inspected = await service.inspect_workspace(Workspace(root_path=str(root)))

            self.assertEqual(inspected.root_path, str(root.resolve()))
            self.assertIsNone(inspected.branch)
            self.assertIsNone(inspected.commit_sha)

    async def test_invalid_workspace_path_maps_to_typed_error(self):
        service = LocalRepoIntelligenceService()
        missing = Path(tempfile.gettempdir()) / "pyclaw-step1-missing-workspace"

        with self.assertRaises(ErrorCodeContractError) as context:
            await service.inspect_workspace(Workspace(root_path=str(missing)))

        self.assertEqual(context.exception.error_code, ErrorCode.WORKSPACE_NOT_FOUND)

    async def test_build_context_returns_repo_context_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._init_git_repo(root)
            (root / "README.md").write_text("# demo\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            (root / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
            self._commit_all(root, "initial context")

            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))
            result = await service.build_context(
                RepoContextRequest(
                    workspace_id=workspace.workspace_id,
                    run_id=new_run_id(),
                    prompt="Update main",
                    target_paths=("app.py",),
                    max_files=4,
                )
            )

            self.assertEqual(result.workspace_id, workspace.workspace_id)
            self.assertTrue(result.file_summaries)
            self.assertEqual(result.file_summaries[0].path, "app.py")
            self.assertIsInstance(result.warnings, tuple)

    async def test_summarize_files_handles_text_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")

            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))
            summaries = await service.summarize_files(workspace, ("main.py",))

            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].path, "main.py")
            self.assertIn("bytes", summaries[0].summary or "")
            self.assertIn(summaries[0].language, {"py", "python"})

    async def test_binary_and_oversized_files_are_skipped_or_warned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._init_git_repo(root)
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "blob.bin").write_bytes(b"\x00\x01\x02")
            (root / "huge.txt").write_text("x" * (1024 * 1024 + 32), encoding="utf-8")
            self._commit_all(root, "mixed files")

            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))
            summaries = await service.summarize_files(workspace, ("main.py", "blob.bin", "huge.txt"))
            by_path = {summary.path: summary for summary in summaries}

            self.assertIn(ErrorCode.WORKSPACE_BINARY_FILE.value, by_path["blob.bin"].summary or "")
            self.assertIn(ErrorCode.WORKSPACE_FILE_TOO_LARGE.value, by_path["huge.txt"].summary or "")

            result = await service.build_context(
                RepoContextRequest(
                    workspace_id=workspace.workspace_id,
                    run_id=new_run_id(),
                    target_paths=("main.py", "blob.bin", "huge.txt"),
                    max_files=4,
                )
            )
            joined_warnings = "\n".join(result.warnings)
            self.assertIn(ErrorCode.WORKSPACE_BINARY_FILE.value, joined_warnings)
            self.assertIn(ErrorCode.WORKSPACE_FILE_TOO_LARGE.value, joined_warnings)

    def test_existing_cli_imports_still_work(self):
        imported = importlib.import_module("apps.cli.app")
        self.assertIsNotNone(imported)

    def test_repo_intelligence_imports_avoid_circular_dependency(self):
        package = importlib.import_module("services.repo_intelligence")
        module = importlib.import_module("services.repo_intelligence.local")

        self.assertIs(package.LocalRepoIntelligenceService, LocalRepoIntelligenceService)
        self.assertTrue(hasattr(module, "LocalRepoIntelligenceService"))


if __name__ == "__main__":
    unittest.main()
