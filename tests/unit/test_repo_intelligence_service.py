from __future__ import annotations

import importlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from packages.shared_types import ErrorCode, ErrorCodeContractError, RepoContextRequest, Workspace
from packages.shared_types.ids import new_run_id
from services.repo_intelligence import RepoIntelligenceService
from services.repo_intelligence import local as repo_intelligence_local
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

    async def test_inspect_workspace_rejects_workspace_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            real_root = tmp_root / "workspace"
            real_root.mkdir()
            symlink_root = tmp_root / "workspace-link"
            try:
                symlink_root.symlink_to(real_root, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks are unavailable in this environment")

            service = LocalRepoIntelligenceService()
            with self.assertRaises(ErrorCodeContractError) as context:
                await service.inspect_workspace(Workspace(root_path=str(symlink_root)))

            self.assertEqual(context.exception.error_code, ErrorCode.WORKSPACE_SYMLINK_ESCAPE)

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

    async def test_build_context_rejects_symlink_target_outside_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            repo_root = tmp_root / "repo"
            repo_root.mkdir()
            outside_file = tmp_root / "outside.py"
            outside_file.write_text("print('nope')\n", encoding="utf-8")
            leak_link = repo_root / "leak.py"
            try:
                leak_link.symlink_to(outside_file)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks are unavailable in this environment")

            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(repo_root)))

            with self.assertRaises(ErrorCodeContractError) as context:
                await service.build_context(
                    RepoContextRequest(
                        workspace_id=workspace.workspace_id,
                        run_id=new_run_id(),
                        target_paths=("leak.py",),
                    )
                )

            self.assertEqual(context.exception.error_code, ErrorCode.WORKSPACE_SYMLINK_ESCAPE)

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

    async def test_build_context_suppresses_background_binary_and_generated_warnings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._init_git_repo(root)
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
            website_assets = root / "website" / "assets"
            website_assets.mkdir(parents=True)
            (website_assets / "image.jpg").write_bytes(b"\xff\xd8\xff")
            vendor_dir = root / "vendor"
            vendor_dir.mkdir()
            (vendor_dir / "bundle.min.js").write_text("console.log('x')\n", encoding="utf-8")
            self._commit_all(root, "context with ignored assets")

            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))
            result = await service.build_context(
                RepoContextRequest(
                    workspace_id=workspace.workspace_id,
                    run_id=new_run_id(),
                    target_paths=("main.py",),
                    max_files=4,
                )
            )

            joined_warnings = "\n".join(result.warnings)
            self.assertNotIn("image.jpg", joined_warnings)
            self.assertNotIn("bundle.min.js", joined_warnings)
            self.assertNotIn(ErrorCode.WORKSPACE_BINARY_FILE.value, joined_warnings)
            self.assertNotIn(ErrorCode.WORKSPACE_GENERATED_OR_VENDOR_FILE.value, joined_warnings)

    async def test_search_symbols_returns_matching_tags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "main.py").write_text("def worker_service():\n    return 1\n", encoding="utf-8")
            (root / "other.py").write_text("class Worker:\n    pass\n", encoding="utf-8")

            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))

            class FakeTag:
                def __init__(self, name: str, kind: str, line: int):
                    self.name = name
                    self.kind = kind
                    self.line = line

            class FakeRepoMap:
                def get_tags(self, _fname: str, rel_fname: str):
                    if rel_fname == "main.py":
                        return [FakeTag("worker_service", "function", 0)]
                    if rel_fname == "other.py":
                        return [FakeTag("Worker", "class", 0)]
                    return []

            with patch.object(service, "_new_repo_map", return_value=FakeRepoMap()):
                matches = await service.search_symbols(workspace, "worker")

            by_name = {match.name: match for match in matches}
            self.assertEqual(set(by_name), {"worker_service", "Worker"})
            self.assertEqual(by_name["worker_service"].path, "main.py")
            self.assertEqual(by_name["worker_service"].line, 1)

    async def test_search_symbols_deduplicates_identical_matches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "main.py").write_text("def worker_service():\n    return 1\n", encoding="utf-8")

            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))

            class FakeTag:
                def __init__(self, name: str, kind: str, line: int):
                    self.name = name
                    self.kind = kind
                    self.line = line

            class FakeRepoMap:
                def get_tags(self, _fname: str, _rel_fname: str):
                    return [
                        FakeTag("worker_service", "function", 0),
                        FakeTag("worker_service", "function", 0),
                    ]

            with patch.object(service, "_new_repo_map", return_value=FakeRepoMap()):
                matches = await service.search_symbols(workspace, "worker")

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].name, "worker_service")

    async def test_search_symbols_scans_beyond_old_default_file_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))

            candidate_paths = []
            for index in range(80):
                path = root / f"file_{index:03d}.py"
                path.write_text("pass\n", encoding="utf-8")
                candidate_paths.append(path)
            target_path = root / "services" / "repomap.py"
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text("class RepoMap:\n    pass\n", encoding="utf-8")
            candidate_paths.append(target_path)

            class FakeTag:
                def __init__(self, name: str, kind: str, line: int):
                    self.name = name
                    self.kind = kind
                    self.line = line

            class FakeRepoMap:
                def get_tags(self, _fname: str, rel_fname: str):
                    if rel_fname == "services/repomap.py":
                        return [FakeTag("RepoMap", "class", 0)]
                    return []

            with (
                patch.object(service, "_list_workspace_files", return_value=(candidate_paths, [])),
                patch.object(service, "_new_repo_map", return_value=FakeRepoMap()),
            ):
                matches = await service.search_symbols(workspace, "RepoMap")

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].path, "services/repomap.py")

    async def test_search_symbols_prioritizes_service_and_package_scopes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))

            rel_paths = (
                "pyclaw/repomap.py",
                "tests/test_repomap.py",
                "packages/shared_types/repomap.py",
                "services/repo_intelligence/repomap.py",
            )
            candidate_paths = []
            for rel_path in rel_paths:
                path = root / rel_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("class RepoMap:\n    pass\n", encoding="utf-8")
                candidate_paths.append(path)

            class FakeTag:
                def __init__(self, name: str, kind: str, line: int):
                    self.name = name
                    self.kind = kind
                    self.line = line

            class FakeRepoMap:
                def get_tags(self, _fname: str, _rel_fname: str):
                    return [FakeTag("RepoMap", "def", 0)]

            with (
                patch.object(service, "_list_workspace_files", return_value=(candidate_paths, [])),
                patch.object(service, "_new_repo_map", return_value=FakeRepoMap()),
            ):
                matches = await service.search_symbols(workspace, "RepoMap")

            self.assertEqual(
                [match.path for match in matches[:4]],
                [
                    "services/repo_intelligence/repomap.py",
                    "packages/shared_types/repomap.py",
                    "pyclaw/repomap.py",
                    "tests/test_repomap.py",
                ],
            )

    async def test_analyze_impact_finds_importers_and_dependencies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "helpers.py").write_text("def util():\n    return 1\n", encoding="utf-8")
            (pkg / "api.py").write_text(
                "from .helpers import util\n\nclass Service:\n    pass\n",
                encoding="utf-8",
            )
            (root / "consumer.py").write_text(
                "from pkg.api import Service\n\nvalue = Service\n",
                encoding="utf-8",
            )

            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))
            impact = await service.analyze_impact(workspace, ("pkg/api.py",))

            self.assertEqual(impact.changed_paths, ("pkg/api.py",))
            self.assertIn("pkg/helpers.py", impact.impacted_paths)
            self.assertIn("consumer.py", impact.impacted_paths)
            self.assertIn("pkg/api.py", impact.impacted_paths)

    async def test_new_repo_map_uses_repo_intelligence_implementation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))

            repo_map = service._new_repo_map(workspace)

            from services.repo_intelligence.repomap import RepoMap

            self.assertIsInstance(repo_map, RepoMap)

    async def test_new_repo_map_propagates_initialization_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))

            # Verifies that a real RepoMap construction bug fails loudly.
            # This catches broad fallback logic that would hide broken initialization.
            # Propagating the RuntimeError is correct because this is not an optional import case.
            with patch.object(
                importlib.import_module("services.repo_intelligence.repomap"),
                "RepoMap",
                side_effect=RuntimeError("broken repo map"),
            ):
                with self.assertRaisesRegex(RuntimeError, "broken repo map"):
                    service._new_repo_map(workspace)

    async def test_refresh_index_invalidates_changed_file_analysis_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "api.py").write_text("class Service:\n    pass\n", encoding="utf-8")
            consumer = root / "consumer.py"
            consumer.write_text("value = 1\n", encoding="utf-8")

            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))

            initial = await service.analyze_impact(workspace, ("pkg/api.py",))
            self.assertNotIn("consumer.py", initial.impacted_paths)

            consumer.write_text("from pkg.api import Service\n\nvalue = Service\n", encoding="utf-8")
            await service.refresh_index(workspace, ("consumer.py",))

            refreshed = await service.analyze_impact(workspace, ("pkg/api.py",))
            self.assertIn("consumer.py", refreshed.impacted_paths)

    async def test_refresh_index_clears_analysis_cache_on_branch_switch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._init_git_repo(root)
            pkg = root / "pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "api.py").write_text("class Service:\n    pass\n", encoding="utf-8")
            consumer = root / "consumer.py"
            consumer.write_text("value = 1\n", encoding="utf-8")
            self._commit_all(root, "main branch baseline")

            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))

            initial = await service.analyze_impact(workspace, ("pkg/api.py",))
            self.assertNotIn("consumer.py", initial.impacted_paths)

            subprocess.run(
                ["git", "-C", str(root), "checkout", "-b", "feature/cache-reset"],
                check=True,
                capture_output=True,
                text=True,
            )
            consumer.write_text("from pkg.api import Service\n\nvalue = Service\n", encoding="utf-8")
            await service.refresh_index(workspace, ())

            refreshed = await service.analyze_impact(workspace, ("pkg/api.py",))
            self.assertIn("consumer.py", refreshed.impacted_paths)

    async def test_watch_workspace_returns_descriptor_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (root / "README.md").write_text("# demo\n", encoding="utf-8")
            (root / "ignored.txt").write_text("skip\n", encoding="utf-8")
            src_dir = root / "src"
            src_dir.mkdir()
            (src_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")

            service = LocalRepoIntelligenceService()
            workspace = await service.inspect_workspace(Workspace(root_path=str(root)))
            subscription = await service.watch_workspace(workspace)
            resolved_root = Path(workspace.root_path)

            self.assertEqual(subscription.workspace_id, workspace.workspace_id)
            self.assertTrue(subscription.subscription_id.startswith("watch_"))
            self.assertIn(str(resolved_root / "README.md"), subscription.watched_paths)
            self.assertIn(str(resolved_root / ".gitignore"), subscription.watched_paths)
            self.assertIn(str(resolved_root / "src"), subscription.watched_paths)
            if repo_intelligence_local._PathSpec is not None:
                self.assertNotIn(str(resolved_root / "ignored.txt"), subscription.watched_paths)

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
