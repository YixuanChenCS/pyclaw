from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import AsyncIterator, Sequence
from urllib.parse import unquote, urlparse

from packages.provider_adapters import DeploymentAdapter
from packages.shared_types import (
    ApprovalDecision,
    ApprovalId,
    ApprovalRecord,
    ApprovalRequest,
    Artifact,
    ArtifactType,
    DeploymentRequest,
    DeploymentResult,
    EntityNotFoundError,
    ErrorCode,
    ErrorCodeContractError,
    HealthCheckResult,
    InvalidRunStateError,
    RecoveryStatus,
    RunEvent,
    RunId,
    RunRequest,
    RunResult,
    RunStatus,
    Session,
    SessionId,
    Workspace,
)
from services.agent_core import (
    AgentCoreCoordinator,
    LocalAgentRunnerConfig,
    build_local_agent_runner_stack,
    resolve_local_agent_runner_config,
)
from services.agent_core.validation import AgentStateValidationError
from services.execution_runtime import ExecutionRuntimeService, SQLiteExecutionRuntimeRepository
from services.ops_observability import OpsObservabilityService
from services.repo_intelligence import LocalRepoIntelligenceService, RepoIntelligenceService

from apps._local_support import (
    NoopObservabilityService,
    WorkspaceRegistryRepoStore,
    synthesize_run_result,
)
from apps.api.platform_api.base import ArtifactDownload, PlatformAPI
from apps.api.schemas import ArtifactDetailResponse


_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.CANCELLED,
        RunStatus.FAILED,
        RunStatus.SUCCEEDED,
    }
)

_MAX_INLINE_ARTIFACT_BYTES = 64 * 1024
_MAX_CONTENT_SNIFF_BYTES = 4096


def _artifact_download_uri(artifact_id: str) -> str:
    return f"/artifacts/{artifact_id}/download"


def artifact_detail_from_model(artifact: Artifact) -> ArtifactDetailResponse:
    return ArtifactDetailResponse(
        artifact_id=str(artifact.artifact_id),
        run_id=str(artifact.run_id),
        artifact_type=artifact.artifact_type.value,
        label=artifact.label,
        uri=artifact.uri,
        created_at=artifact.created_at.isoformat().replace("+00:00", "Z"),
        size_bytes=None,
        content=None,
        content_inline=False,
        content_kind=None,
        content_note="Artifact content is not available inlined.",
        download_uri=None,
    )


class LocalPlatformAPIAdapter(PlatformAPI):
    def __init__(
        self,
        *,
        agent_core,
        execution_runtime: ExecutionRuntimeService,
        repo_intelligence: RepoIntelligenceService,
        observability: OpsObservabilityService,
        coordinator: AgentCoreCoordinator | None = None,
    ) -> None:
        self._agent_core = agent_core
        self._execution_runtime = execution_runtime
        self._repo_intelligence = repo_intelligence
        self._observability = observability
        self._workspace_store = getattr(execution_runtime, "_repo_store", None)
        self._coordinator = coordinator or AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=execution_runtime,
            session_store=getattr(execution_runtime, "repository", None),
            repo_intelligence=repo_intelligence,
            repo_store=getattr(execution_runtime, "_repo_store", None),
        )

    async def create_run(self, request: RunRequest) -> str:
        return await self._execution_runtime.enqueue_run(request)

    async def create_run_from_workspace(
        self,
        *,
        workspace_path: str,
        prompt: str,
        target_paths: Sequence[str] = (),
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        workspace = Workspace(root_path=workspace_path)
        inspected = await self._repo_intelligence.inspect_workspace(workspace)
        register_workspace = getattr(self._workspace_store, "register_workspace", None)
        if callable(register_workspace):
            register_workspace(inspected)
        session = Session(
            workspace_id=inspected.workspace_id,
            session_id=SessionId(session_id) if session_id is not None else SessionId.generate(),
            title="api",
        )
        return await self._execution_runtime.enqueue_run(
            RunRequest(
                run_id=RunId(run_id) if run_id is not None else None,
                workspace_id=inspected.workspace_id,
                session_id=session.session_id,
                prompt=prompt,
                target_paths=tuple(target_paths),
            )
        )

    async def list_runs(
        self,
        workspace_id: str | None = None,
        *,
        session_id: str | None = None,
        status: RunStatus | str | None = None,
        limit: int | None = None,
    ) -> Sequence[RunResult]:
        repository = self._require_repository()
        runs = await repository.list_runs(
            workspace_id,
            session_id=session_id,
            status=status,
        )
        results: list[RunResult] = []
        for run in runs:
            result = await synthesize_run_result(repository, str(run.run_id))
            if result is not None:
                results.append(result)
        if limit is not None:
            return tuple(results[:limit])
        return tuple(results)

    async def get_run(self, run_id: str) -> RunResult | None:
        return await synthesize_run_result(self._require_repository(), run_id)

    async def get_run_summary(self, run_id: str) -> RunResult | None:
        return await self.get_run(run_id)

    async def list_run_events(self, run_id: str) -> Sequence[RunEvent]:
        repository = self._require_repository()
        run = await repository.get_run(run_id)
        if run is None:
            raise EntityNotFoundError("run", run_id)
        return await repository.list_events(run.run_id)

    async def list_artifacts(self, run_id: str) -> Sequence[ArtifactDetailResponse]:
        repository = self._require_repository()
        run = await repository.get_run(run_id)
        if run is None:
            raise EntityNotFoundError("run", run_id)
        artifacts = await repository.list_artifacts(run.run_id)
        run_root = await self._workspace_root_for_run(run_id)
        return tuple(
            [await self._build_artifact_detail(artifact, run_root=run_root) for artifact in artifacts]
        )

    async def get_artifact(self, artifact_id: str) -> ArtifactDetailResponse | None:
        repository = self._require_repository()
        artifact = await repository.get_artifact(artifact_id)
        if artifact is None:
            return None
        run_root = await self._workspace_root_for_run(str(artifact.run_id))
        return await self._build_artifact_detail(artifact, run_root=run_root)

    async def get_artifact_download(self, artifact_id: str) -> ArtifactDownload | None:
        repository = self._require_repository()
        artifact = await repository.get_artifact(artifact_id)
        if artifact is None:
            return None
        if artifact.artifact_type == ArtifactType.PATCH:
            raise ErrorCodeContractError(
                ErrorCode.INVALID_REQUEST,
                f"Artifact {artifact_id} does not expose a downloadable file.",
            )
        run_root = await self._workspace_root_for_run(str(artifact.run_id))
        resolved = await self._resolve_artifact_file(artifact, run_root=run_root)
        if resolved is None:
            raise ErrorCodeContractError(
                ErrorCode.INVALID_REQUEST,
                f"Artifact {artifact_id} does not expose a downloadable file.",
            )
        return ArtifactDownload(
            artifact_id=str(artifact.artifact_id),
            path=resolved,
            media_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
            filename=artifact.label or resolved.name,
        )

    async def get_recovery_status(self, run_id: str) -> RecoveryStatus | None:
        repository = self._require_repository()
        run = await repository.get_run(run_id)
        if run is None:
            raise EntityNotFoundError("run", run_id)
        return await self._execution_runtime.get_recovery_status(run_id)

    async def rollback_recovery(self, run_id: str, task_id: str) -> RecoveryStatus:
        repository = self._require_repository()
        run = await repository.get_run(run_id)
        if run is None:
            raise EntityNotFoundError("run", run_id)
        return await self._execution_runtime.rollback_task(run_id, task_id)

    async def get_operability_snapshot(self) -> dict[str, object]:
        health = await self.get_health()
        runtime = health.details.get("runtime")
        runtime_details = {}
        if isinstance(runtime, dict):
            details = runtime.get("details")
            if isinstance(details, dict):
                runtime_details = dict(details)
            runtime_details["status"] = str(runtime.get("status", "unknown"))
        return {
            "service": health.service,
            "status": health.status,
            "runtime": runtime_details,
        }

    async def list_approvals(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> Sequence[ApprovalRecord]:
        repository = self._require_repository()
        approvals = await repository.list_approvals(run_id=run_id)
        if status is None:
            return approvals
        return tuple(approval for approval in approvals if approval.status == status)

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        return await self._require_repository().get_approval(approval_id)

    async def stream_run_events(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
    ) -> AsyncIterator[RunEvent]:
        after_sequence = await self._resolve_event_checkpoint(run_id, last_event_id)
        async for event in self._execution_runtime.stream_events(
            run_id,
            after_sequence=after_sequence,
        ):
            yield event

    async def create_approval_request(self, request: ApprovalRequest) -> str:
        return await self._execution_runtime.request_approval(str(request.run_id), request)

    async def submit_approval(self, decision: ApprovalDecision) -> None:
        session = await self._require_repository().load_agent_session(decision.run_id)
        if session is None or str(session.pending_approval_id or "") != str(decision.approval_id):
            raise ErrorCodeContractError(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"Pending approval was not found: {decision.approval_id}",
                details={
                    "approval_id": str(decision.approval_id),
                    "run_id": str(decision.run_id),
                },
            )
        await self._coordinator.resume_after_approval(
            str(decision.run_id),
            approved=decision.approved,
            reviewer=decision.reviewer,
            comment=decision.comment,
        )

    async def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        comment: str | None = None,
    ) -> ApprovalRecord:
        repository = self._require_repository()
        approval = await repository.get_approval(approval_id)
        if approval is None:
            raise EntityNotFoundError("approval", approval_id)
        if approval.status != "pending":
            raise InvalidRunStateError(
                f"Approval {approval_id} is already finalized with status {approval.status}"
            )
        try:
            await self._coordinator.resume_after_approval(
                str(approval.run_id),
                approved=approved,
                comment=comment,
            )
        except AgentStateValidationError as exc:
            raise InvalidRunStateError(str(exc)) from exc
        updated = await repository.get_approval(ApprovalId(approval_id))
        if updated is None:
            raise EntityNotFoundError("approval", approval_id)
        return updated

    async def cancel_run(self, run_id: str) -> None:
        await self._execution_runtime.cancel_run(run_id)

    async def trigger_deployment(self, request: DeploymentRequest) -> DeploymentResult:
        return await self._execution_runtime.deploy(request)

    async def get_health(self) -> HealthCheckResult:
        runtime_health = await self._execution_runtime.get_health()
        observability_health = await self._observability.get_health()
        status = (
            "ready"
            if runtime_health.status == "ready" and observability_health.status == "ready"
            else "not_ready"
        )
        return HealthCheckResult(
            service="platform-api",
            status=status,
            details={
                "runtime": runtime_health.to_dict(),
                "observability": observability_health.to_dict(),
            },
        )

    async def _resolve_event_checkpoint(
        self,
        run_id: str,
        last_event_id: str | None,
    ) -> int:
        if last_event_id is None:
            return 0
        try:
            sequence = int(last_event_id)
        except ValueError:
            repository = self._require_repository()
            run = await repository.get_run(run_id)
            if run is None:
                raise EntityNotFoundError("run", run_id)
            resolved = await repository.get_event_sequence(run_id, last_event_id)
            if resolved is None:
                raise ErrorCodeContractError(
                    ErrorCode.EVENT_REPLAY_GAP,
                    f"Last event id was not found for run {run_id}: {last_event_id}",
                    details={"run_id": run_id, "last_event_id": last_event_id},
                )
            return resolved
        if sequence < 0:
            raise ErrorCodeContractError(
                ErrorCode.INVALID_REQUEST,
                "Last event sequence must be non-negative.",
                details={"last_event_id": last_event_id},
            )
        return sequence

    async def _build_artifact_detail(
        self,
        artifact: Artifact,
        *,
        run_root: Path | None,
    ) -> ArtifactDetailResponse:
        if artifact.artifact_type == ArtifactType.PATCH:
            patch_diff = await self._resolve_patch_artifact_content(artifact)
            if patch_diff is not None:
                return self._detail_with_inline_text(
                    artifact,
                    content=patch_diff,
                    content_kind="text",
                )

        resolved_file = await self._resolve_artifact_file(artifact, run_root=run_root)
        if resolved_file is not None:
            return self._detail_from_file(artifact, resolved_file)

        return artifact_detail_from_model(artifact).model_copy(
            update={
                "content_note": "Artifact content is not available in the local runtime store.",
            }
        )

    def _detail_from_file(self, artifact: Artifact, path: Path) -> ArtifactDetailResponse:
        size_bytes = path.stat().st_size
        download_uri = _artifact_download_uri(str(artifact.artifact_id))
        sample = self._read_sample_bytes(path)
        content_kind = self._detect_content_kind(
            sample,
            path=path,
            label=artifact.label,
            uri=artifact.uri,
        )
        if content_kind == "binary":
            return artifact_detail_from_model(artifact).model_copy(
                update={
                    "size_bytes": size_bytes,
                    "content_kind": "binary",
                    "content_note": "Binary artifact is not inlined.",
                    "download_uri": download_uri,
                }
            )
        if size_bytes > _MAX_INLINE_ARTIFACT_BYTES:
            return artifact_detail_from_model(artifact).model_copy(
                update={
                    "size_bytes": size_bytes,
                    "content_kind": content_kind,
                    "content_note": f"Artifact exceeds the max inline size of {_MAX_INLINE_ARTIFACT_BYTES} bytes.",
                    "download_uri": download_uri,
                }
            )

        raw = path.read_bytes()
        text = raw.decode("utf-8")
        if self._should_treat_as_json(text, path=path, label=artifact.label, uri=artifact.uri):
            try:
                content = json.loads(text)
            except json.JSONDecodeError:
                content_kind = "text"
                content = text
            else:
                content_kind = "json"
        else:
            content = text
            content_kind = "text"
        return artifact_detail_from_model(artifact).model_copy(
            update={
                "size_bytes": size_bytes,
                "content": content,
                "content_inline": True,
                "content_kind": content_kind,
                "content_note": None,
                "download_uri": download_uri,
            }
        )

    def _detail_with_inline_text(
        self,
        artifact: Artifact,
        *,
        content: str,
        content_kind: str,
    ) -> ArtifactDetailResponse:
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_INLINE_ARTIFACT_BYTES:
            return artifact_detail_from_model(artifact).model_copy(
                update={
                    "size_bytes": len(encoded),
                    "content_kind": content_kind,
                    "content_note": f"Artifact exceeds the max inline size of {_MAX_INLINE_ARTIFACT_BYTES} bytes.",
                }
            )
        return artifact_detail_from_model(artifact).model_copy(
            update={
                "size_bytes": len(encoded),
                "content": content,
                "content_inline": True,
                "content_kind": content_kind,
                "content_note": None,
            }
        )

    async def _resolve_patch_artifact_content(self, artifact: Artifact) -> str | None:
        repository = self._require_repository()
        session = await repository.load_agent_session(artifact.run_id)
        if session is None:
            return None
        actions = [action for action in session.action_history if isinstance(action.patch_diff, str) and action.patch_diff.strip()]
        if session.pending_action is not None and isinstance(session.pending_action.patch_diff, str) and session.pending_action.patch_diff.strip():
            actions.append(session.pending_action)
        if artifact.task_id is not None:
            for action in reversed(actions):
                if str(action.action_id or "") == str(artifact.task_id):
                    return action.patch_diff
        if len(actions) == 1:
            return actions[0].patch_diff
        return None

    async def _resolve_artifact_file(
        self,
        artifact: Artifact,
        *,
        run_root: Path | None,
    ) -> Path | None:
        raw_uri = (artifact.uri or "").strip()
        if not raw_uri:
            return None
        if artifact.artifact_type == ArtifactType.PATCH:
            return None
        parsed = urlparse(raw_uri)
        if parsed.scheme not in ("", "file"):
            return None
        raw_path = unquote(parsed.path if parsed.scheme == "file" else raw_uri)
        if not raw_path:
            return None
        allowed_roots = self._artifact_allowed_roots(run_root)
        for root in allowed_roots:
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                resolved_root = root.resolve(strict=True)
                resolved_candidate = candidate.resolve(strict=True)
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                continue
            if not resolved_candidate.is_file():
                continue
            if not resolved_candidate.is_relative_to(resolved_root):
                continue
            return resolved_candidate
        return None

    def _artifact_allowed_roots(self, run_root: Path | None) -> tuple[Path, ...]:
        roots: list[Path] = []
        if run_root is not None:
            roots.append(run_root)
        roots.append(self._require_repository().db_path.parent)
        deduped: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            try:
                resolved = str(root.resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            deduped.append(root)
        return tuple(deduped)

    async def _workspace_root_for_run(self, run_id: str) -> Path | None:
        repository = self._require_repository()
        run = await repository.get_run(run_id)
        if run is None:
            return None
        get_workspace = getattr(self._workspace_store, "get_workspace", None)
        if not callable(get_workspace):
            return None
        workspace = await get_workspace(run.workspace_id)
        root_path = getattr(workspace, "root_path", None)
        if not isinstance(root_path, str) or not root_path.strip():
            return None
        try:
            return Path(root_path).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None

    def _read_sample_bytes(self, path: Path) -> bytes:
        with path.open("rb") as handle:
            return handle.read(_MAX_CONTENT_SNIFF_BYTES)

    def _detect_content_kind(
        self,
        sample: bytes,
        *,
        path: Path,
        label: str | None,
        uri: str | None,
    ) -> str:
        if b"\x00" in sample:
            return "binary"
        try:
            text = sample.decode("utf-8")
        except UnicodeDecodeError:
            return "binary"
        if self._should_treat_as_json(text, path=path, label=label, uri=uri):
            return "json"
        return "text"

    def _should_treat_as_json(
        self,
        text: str,
        *,
        path: Path,
        label: str | None,
        uri: str | None,
    ) -> bool:
        candidates = [path.name]
        if label:
            candidates.append(label)
        if uri:
            candidates.append(uri)
        if any(candidate.lower().endswith(".json") for candidate in candidates):
            return True
        stripped = text.lstrip()
        return stripped.startswith("{") or stripped.startswith("[")

    def _require_repository(self) -> SQLiteExecutionRuntimeRepository:
        repository = getattr(self._execution_runtime, "repository", None)
        if not isinstance(repository, SQLiteExecutionRuntimeRepository):
            raise RuntimeError("Platform API local adapter requires a LocalExecutionRuntimeService repository")
        return repository


def create_platform_api(
    agent_core,
    execution_runtime: ExecutionRuntimeService,
    repo_intelligence: RepoIntelligenceService,
    observability: OpsObservabilityService,
    coordinator: AgentCoreCoordinator | None = None,
) -> PlatformAPI:
    return LocalPlatformAPIAdapter(
        agent_core=agent_core,
        execution_runtime=execution_runtime,
        repo_intelligence=repo_intelligence,
        observability=observability,
        coordinator=coordinator,
    )


def create_local_platform_api_from_env(
    *,
    deployment_adapter: DeploymentAdapter | None = None,
) -> PlatformAPI:
    config = resolve_local_agent_runner_config(workspace_root=".")
    return create_local_platform_api_from_config(
        config,
        deployment_adapter=deployment_adapter,
    )


def create_local_platform_api_from_config(
    config: LocalAgentRunnerConfig,
    *,
    deployment_adapter: DeploymentAdapter | None = None,
) -> PlatformAPI:
    workspace_store = WorkspaceRegistryRepoStore()
    repo_intelligence = LocalRepoIntelligenceService()
    observability = NoopObservabilityService()
    stack = build_local_agent_runner_stack(
        config=config,
        repo_store=workspace_store,
        repo_intelligence=repo_intelligence,
        deployment_adapter=deployment_adapter,
    )
    return LocalPlatformAPIAdapter(
        agent_core=stack.agent_core,
        execution_runtime=stack.execution_runtime,
        repo_intelligence=repo_intelligence,
        observability=observability,
        coordinator=stack.coordinator,
    )


__all__ = [
    "LocalPlatformAPIAdapter",
    "artifact_detail_from_model",
    "create_local_platform_api_from_config",
    "create_local_platform_api_from_env",
    "create_platform_api",
]
