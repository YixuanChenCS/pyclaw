from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apps._local_support import NoopObservabilityService, WorkspaceRegistryRepoStore
from apps.api.platform_api import LocalPlatformAPIAdapter
from packages.shared_types import Artifact, ArtifactType, Run, Session, Workspace
from services.agent_core.models import AgentAction, AgentActionType, AgentSession
from services.execution_runtime import SQLiteExecutionRuntimeRepository


class _RuntimeStub:
    def __init__(self, repository: SQLiteExecutionRuntimeRepository, workspace_store: WorkspaceRegistryRepoStore) -> None:
        self.repository = repository
        self._repo_store = workspace_store


class TestLocalPlatformAPIArtifactResolution(unittest.IsolatedAsyncioTestCase):
    async def test_get_artifact_inlines_small_text_and_json_and_exposes_large_download(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace_root = root / "workspace"
            runtime_root = root / ".execution_runtime"
            workspace_root.mkdir()
            runtime_root.mkdir()

            repository = SQLiteExecutionRuntimeRepository(runtime_root / "runtime.sqlite3")
            workspace_store = WorkspaceRegistryRepoStore()
            workspace = Workspace(root_path=str(workspace_root))
            workspace_store.register_workspace(workspace)

            run = Run(
                workspace_id=workspace.workspace_id,
                session_id=Session(workspace_id=workspace.workspace_id, title="api").session_id,
                prompt="Inspect artifacts",
            )
            await repository.create_run(run)

            text_path = workspace_root / "artifacts" / "stdout.log"
            text_path.parent.mkdir()
            text_path.write_text("hello world\n", encoding="utf-8")
            text_artifact = Artifact(
                run_id=run.run_id,
                artifact_type=ArtifactType.LOG,
                label="stdout.log",
                uri="artifacts/stdout.log",
            )
            await repository.create_artifact(text_artifact)

            json_path = workspace_root / "artifacts" / "report.json"
            json_path.write_text('{"ok": true, "count": 1}', encoding="utf-8")
            json_artifact = Artifact(
                run_id=run.run_id,
                artifact_type=ArtifactType.SUMMARY,
                label="report.json",
                uri="artifacts/report.json",
            )
            await repository.create_artifact(json_artifact)

            large_path = runtime_root / "command-output.txt"
            large_path.write_text("a" * 70000, encoding="utf-8")
            large_artifact = Artifact(
                run_id=run.run_id,
                artifact_type=ArtifactType.COMMAND_OUTPUT,
                label="command-output.txt",
                uri=str(large_path),
            )
            await repository.create_artifact(large_artifact)

            adapter = LocalPlatformAPIAdapter(
                agent_core=object(),
                execution_runtime=_RuntimeStub(repository, workspace_store),
                repo_intelligence=object(),
                observability=NoopObservabilityService(),
                coordinator=object(),
            )

            resolved_text = await adapter.get_artifact(str(text_artifact.artifact_id))
            resolved_json = await adapter.get_artifact(str(json_artifact.artifact_id))
            resolved_large = await adapter.get_artifact(str(large_artifact.artifact_id))

            assert resolved_text is not None
            assert resolved_text.content == "hello world\n"
            assert resolved_text.content_inline is True
            assert resolved_text.content_kind == "text"
            assert resolved_text.size_bytes == len("hello world\n".encode("utf-8"))
            assert resolved_text.download_uri == f"/artifacts/{text_artifact.artifact_id}/download"

            assert resolved_json is not None
            assert resolved_json.content == {"ok": True, "count": 1}
            assert resolved_json.content_inline is True
            assert resolved_json.content_kind == "json"
            assert resolved_json.download_uri == f"/artifacts/{json_artifact.artifact_id}/download"

            assert resolved_large is not None
            assert resolved_large.content is None
            assert resolved_large.content_inline is False
            assert resolved_large.content_kind == "text"
            assert resolved_large.size_bytes == 70000
            assert resolved_large.download_uri == f"/artifacts/{large_artifact.artifact_id}/download"
            assert "max inline size" in (resolved_large.content_note or "")

    async def test_get_artifact_marks_binary_file_as_non_inline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace_root = root / "workspace"
            runtime_root = root / ".execution_runtime"
            workspace_root.mkdir()
            runtime_root.mkdir()

            repository = SQLiteExecutionRuntimeRepository(runtime_root / "runtime.sqlite3")
            workspace_store = WorkspaceRegistryRepoStore()
            workspace = Workspace(root_path=str(workspace_root))
            workspace_store.register_workspace(workspace)

            run = Run(
                workspace_id=workspace.workspace_id,
                session_id=Session(workspace_id=workspace.workspace_id, title="api").session_id,
                prompt="Inspect binary artifact",
            )
            await repository.create_run(run)

            binary_path = runtime_root / "payload.bin"
            binary_path.write_bytes(b"\x00\x01\x02")
            artifact = Artifact(
                run_id=run.run_id,
                artifact_type=ArtifactType.TEST_RESULT,
                label="payload.bin",
                uri=str(binary_path),
            )
            await repository.create_artifact(artifact)

            adapter = LocalPlatformAPIAdapter(
                agent_core=object(),
                execution_runtime=_RuntimeStub(repository, workspace_store),
                repo_intelligence=object(),
                observability=NoopObservabilityService(),
                coordinator=object(),
            )

            resolved = await adapter.get_artifact(str(artifact.artifact_id))

            assert resolved is not None
            assert resolved.content is None
            assert resolved.content_inline is False
            assert resolved.content_kind == "binary"
            assert resolved.download_uri == f"/artifacts/{artifact.artifact_id}/download"
            assert resolved.content_note == "Binary artifact is not inlined."

    async def test_get_artifact_recovers_patch_diff_from_agent_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace_root = root / "workspace"
            runtime_root = root / ".execution_runtime"
            workspace_root.mkdir()
            runtime_root.mkdir()

            repository = SQLiteExecutionRuntimeRepository(runtime_root / "runtime.sqlite3")
            workspace_store = WorkspaceRegistryRepoStore()
            workspace = Workspace(root_path=str(workspace_root))
            workspace_store.register_workspace(workspace)

            run = Run(
                workspace_id=workspace.workspace_id,
                session_id=Session(workspace_id=workspace.workspace_id, title="api").session_id,
                prompt="Recover patch artifact",
            )
            await repository.create_run(run)

            artifact = Artifact(
                run_id=run.run_id,
                task_id="task_patch",
                artifact_type=ArtifactType.PATCH,
                label="Patch applied",
                uri="src/app.py",
            )
            await repository.create_artifact(artifact)

            patch_diff = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
            session = AgentSession(
                run_id=run.run_id,
                workspace_id=workspace.workspace_id,
                user_request="Recover patch",
                action_history=[
                    AgentAction(
                        type=AgentActionType.PROPOSE_PATCH,
                        reason="Apply patch",
                        action_id="task_patch",
                        patch_diff=patch_diff,
                    )
                ],
            )
            await repository.save_agent_session(session)

            adapter = LocalPlatformAPIAdapter(
                agent_core=object(),
                execution_runtime=_RuntimeStub(repository, workspace_store),
                repo_intelligence=object(),
                observability=NoopObservabilityService(),
                coordinator=object(),
            )

            resolved = await adapter.get_artifact(str(artifact.artifact_id))

            assert resolved is not None
            assert resolved.content == patch_diff
            assert resolved.content_inline is True
            assert resolved.content_kind == "text"
            assert resolved.download_uri is None

    async def test_get_artifact_does_not_read_paths_outside_workspace_or_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace_root = root / "workspace"
            runtime_root = root / ".execution_runtime"
            outside_root = root / "outside"
            workspace_root.mkdir()
            runtime_root.mkdir()
            outside_root.mkdir()

            repository = SQLiteExecutionRuntimeRepository(runtime_root / "runtime.sqlite3")
            workspace_store = WorkspaceRegistryRepoStore()
            workspace = Workspace(root_path=str(workspace_root))
            workspace_store.register_workspace(workspace)

            run = Run(
                workspace_id=workspace.workspace_id,
                session_id=Session(workspace_id=workspace.workspace_id, title="api").session_id,
                prompt="Reject escaped artifact path",
            )
            await repository.create_run(run)

            outside_path = outside_root / "secret.txt"
            outside_path.write_text("should-not-be-read", encoding="utf-8")
            artifact = Artifact(
                run_id=run.run_id,
                artifact_type=ArtifactType.LOG,
                label="secret.txt",
                uri=str(outside_path),
            )
            await repository.create_artifact(artifact)

            adapter = LocalPlatformAPIAdapter(
                agent_core=object(),
                execution_runtime=_RuntimeStub(repository, workspace_store),
                repo_intelligence=object(),
                observability=NoopObservabilityService(),
                coordinator=object(),
            )

            resolved = await adapter.get_artifact(str(artifact.artifact_id))

            assert resolved is not None
            assert resolved.content is None
            assert resolved.content_inline is False
            assert resolved.download_uri is None
            assert resolved.content_note == "Artifact content is not available in the local runtime store."
