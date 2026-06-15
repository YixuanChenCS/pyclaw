from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator, Literal, Sequence
from typing import Any
from typing import cast

from fastapi.middleware.cors import CORSMiddleware
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator
from packages.provider_adapters import DeploymentAdapter
from packages.shared_types import (
    ApprovalDecision,
    ApprovalId,
    ApprovalRecord,
    ApprovalRequest,
    Artifact,
    DeploymentRequest,
    DeploymentResult,
    EntityNotFoundError,
    ErrorCode,
    ErrorCodeContractError,
    HealthCheckResult,
    InvalidRunStateError,
    RunEvent,
    RunId,
    RunRequest,
    RunResult,
    RunStatus,
    Session,
    SessionId,
    Workspace,
    WorkspaceId,
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


class PlatformAPI:
    """Control-plane API for CLI and dashboard clients."""

    async def create_run(self, request: RunRequest) -> str:
        raise NotImplementedError

    async def create_run_from_workspace(
        self,
        *,
        workspace_path: str,
        prompt: str,
        target_paths: Sequence[str] = (),
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        raise NotImplementedError

    async def list_runs(
        self,
        workspace_id: str | None = None,
        *,
        session_id: str | None = None,
        status: RunStatus | str | None = None,
        limit: int | None = None,
    ) -> Sequence[RunResult]:
        raise NotImplementedError

    async def get_run(self, run_id: str) -> RunResult | None:
        raise NotImplementedError

    async def get_run_summary(self, run_id: str) -> RunResult | None:
        raise NotImplementedError

    async def list_run_events(self, run_id: str) -> Sequence[RunEvent]:
        raise NotImplementedError

    async def list_artifacts(self, run_id: str) -> Sequence["ArtifactDetailResponse"]:
        raise NotImplementedError

    async def get_artifact(self, artifact_id: str) -> "ArtifactDetailResponse | None":
        raise NotImplementedError

    async def list_approvals(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> Sequence[ApprovalRecord]:
        raise NotImplementedError

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        raise NotImplementedError

    async def stream_run_events(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
    ) -> AsyncIterator[RunEvent]:
        raise NotImplementedError

    async def create_approval_request(self, request: ApprovalRequest) -> str:
        raise NotImplementedError

    async def submit_approval(self, decision: ApprovalDecision) -> None:
        raise NotImplementedError

    async def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        comment: str | None = None,
    ) -> ApprovalRecord:
        raise NotImplementedError

    async def cancel_run(self, run_id: str) -> None:
        raise NotImplementedError

    async def trigger_deployment(self, request: DeploymentRequest) -> DeploymentResult:
        raise NotImplementedError

    async def get_health(self) -> HealthCheckResult:
        raise NotImplementedError


class RunCreateRequestBody(BaseModel):
    workspace_id: str | None = Field(default=None, min_length=1)
    workspace_path: str | None = Field(default=None, min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    prompt: str = Field(min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    target_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_workspace_fields(self) -> "RunCreateRequestBody":
        if self.workspace_path is not None:
            return self
        if self.workspace_id is None or self.session_id is None:
            raise ValueError("workspace_path or both workspace_id and session_id must be provided")
        return self

    def to_run_request(self) -> RunRequest:
        if self.workspace_id is None or self.session_id is None:
            raise ValueError("workspace_id and session_id are required when workspace_path is not provided")
        return RunRequest(
            workspace_id=WorkspaceId(self.workspace_id),
            session_id=SessionId(self.session_id),
            prompt=self.prompt,
            run_id=RunId(self.run_id) if self.run_id is not None else None,
            target_paths=tuple(self.target_paths),
        )


class RunAcceptedResponse(BaseModel):
    run_id: str
    status: str


class APIErrorBody(BaseModel):
    code: str
    message: str


class APIErrorResponse(BaseModel):
    error: APIErrorBody


class ApprovalDecisionRequestBody(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str | None = None


class ArtifactSummaryResponse(BaseModel):
    artifact_id: str
    run_id: str
    artifact_type: str
    label: str | None = None
    uri: str | None = None
    size_bytes: int | None = None
    created_at: str | None = None


class ArtifactDetailResponse(ArtifactSummaryResponse):
    content: Any | None = None
    content_inline: bool = False
    content_kind: str | None = None
    content_note: str | None = None


class RunSummaryResponse(BaseModel):
    run_id: str
    status: str
    summary: str | None = None
    completed: bool


_DEFAULT_LOCAL_CORS_ORIGINS = (
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


class APIConfig(BaseModel):
    allowed_workspace_roots: tuple[str, ...] = ()
    api_token: str | None = None
    cors_allowed_origins: tuple[str, ...] = _DEFAULT_LOCAL_CORS_ORIGINS


def create_app(
    *,
    platform_api: PlatformAPI,
    config: APIConfig | None = None,
    title: str = "Pyclaw API",
) -> FastAPI:
    """Create a thin FastAPI application shell over the platform API."""

    api_config = config or APIConfig()
    app = FastAPI(title=title)
    app.state.platform_api = platform_api
    app.state.api_config = api_config

    if api_config.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(api_config.cors_allowed_origins),
            allow_credentials=api_config.api_token is not None,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    router = APIRouter()

    @app.middleware("http")
    async def bearer_token_guard(request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path == "/health":
            return await call_next(request)
        if api_config.api_token is None:
            return await call_next(request)
        auth_header = request.headers.get("Authorization")
        expected = f"Bearer {api_config.api_token}"
        if auth_header != expected:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": {"code": "unauthorized", "message": "Missing or invalid bearer token."}},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and {"code", "message"} <= set(exc.detail):
            return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @router.get("/health", tags=["health"])
    async def get_health(
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> JSONResponse:
        health = await current_platform_api.get_health()
        return JSONResponse(
            status_code=_status_code_for_health(health),
            content=health.to_dict(),
        )

    @router.get("/runs", tags=["runs"])
    async def list_runs(
        workspace_id: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int | None = Query(default=None, ge=1),
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> JSONResponse:
        runs = await current_platform_api.list_runs(
            workspace_id=workspace_id,
            status=status_filter,
            limit=limit,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=[run.to_dict() for run in runs],
        )

    @router.post("/runs", status_code=status.HTTP_202_ACCEPTED, tags=["runs"], response_model=RunAcceptedResponse)
    async def create_run(
        request: Request,
        body: RunCreateRequestBody,
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> RunAcceptedResponse:
        if body.workspace_path is not None:
            canonical_workspace_path = _validate_workspace_path(
                body.workspace_path,
                config=cast(APIConfig, request.app.state.api_config),
            )
            try:
                run_id = await current_platform_api.create_run_from_workspace(
                    workspace_path=canonical_workspace_path,
                    prompt=body.prompt,
                    target_paths=tuple(body.target_paths),
                    run_id=body.run_id,
                    session_id=body.session_id,
                )
            except ErrorCodeContractError as exc:
                raise _workspace_http_exception(exc) from exc
        else:
            run_id = await current_platform_api.create_run(body.to_run_request())
        return RunAcceptedResponse(run_id=run_id, status=RunStatus.QUEUED.value)

    @router.get("/runs/{run_id}", tags=["runs"])
    async def get_run(
        run_id: str,
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> JSONResponse:
        run = await current_platform_api.get_run(run_id)
        if run is None:
            raise _api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=f"run not found: {run_id}",
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=run.to_dict())

    @router.get("/runs/{run_id}/events", tags=["runs"])
    async def list_run_events(
        run_id: str,
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> JSONResponse:
        try:
            events = await current_platform_api.list_run_events(run_id)
        except EntityNotFoundError as exc:
            raise _api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=str(exc),
            ) from exc
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=[event.to_dict() for event in events],
        )

    @router.get("/runs/{run_id}/events/stream", tags=["runs"])
    async def stream_run_events(
        run_id: str,
        request: Request,
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> StreamingResponse:
        if await current_platform_api.get_run(run_id) is None:
            raise _api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=f"run not found: {run_id}",
            )

        async def event_generator():
            async for event in current_platform_api.stream_run_events(run_id):
                if await request.is_disconnected():
                    break
                yield _format_sse_event(event)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @router.get("/runs/{run_id}/artifacts", tags=["artifacts"])
    async def list_run_artifacts(
        run_id: str,
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> JSONResponse:
        try:
            artifacts = await current_platform_api.list_artifacts(run_id)
        except EntityNotFoundError as exc:
            raise _api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=str(exc),
            ) from exc
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=[artifact.model_dump(mode="json") for artifact in artifacts],
        )

    @router.get("/artifacts/{artifact_id}", tags=["artifacts"])
    async def get_artifact(
        artifact_id: str,
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> JSONResponse:
        artifact = await current_platform_api.get_artifact(artifact_id)
        if artifact is None:
            raise _api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=f"artifact not found: {artifact_id}",
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=artifact.model_dump(mode="json"))

    @router.get("/runs/{run_id}/summary", tags=["runs"], response_model=RunSummaryResponse)
    async def get_run_summary(
        run_id: str,
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> RunSummaryResponse:
        run = await current_platform_api.get_run_summary(run_id)
        if run is None:
            raise _api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=f"run not found: {run_id}",
            )
        completed = run.status in _TERMINAL_RUN_STATUSES
        return RunSummaryResponse(
            run_id=str(run.run_id),
            status=run.status.value,
            summary=run.summary if completed else None,
            completed=completed,
        )

    @router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED, tags=["runs"], response_model=RunAcceptedResponse)
    async def cancel_run(
        run_id: str,
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> RunAcceptedResponse:
        try:
            await current_platform_api.cancel_run(run_id)
        except EntityNotFoundError as exc:
            raise _api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=str(exc),
            ) from exc
        except InvalidRunStateError as exc:
            raise _api_http_exception(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.INVALID_STATE_TRANSITION.value,
                message=str(exc),
            ) from exc
        return RunAcceptedResponse(run_id=run_id, status=RunStatus.CANCELLING.value)

    @router.get("/approvals", tags=["approvals"])
    async def list_approvals(
        run_id: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> JSONResponse:
        approvals = await current_platform_api.list_approvals(
            run_id=run_id,
            status=status_filter,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=[approval.to_dict() for approval in approvals],
        )

    @router.get("/approvals/{approval_id}", tags=["approvals"])
    async def get_approval(
        approval_id: str,
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> JSONResponse:
        approval = await current_platform_api.get_approval(approval_id)
        if approval is None:
            raise _api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=f"approval not found: {approval_id}",
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=approval.to_dict())

    @router.post("/approvals/{approval_id}/decision", status_code=status.HTTP_202_ACCEPTED, tags=["approvals"])
    async def decide_approval(
        approval_id: str,
        body: ApprovalDecisionRequestBody,
        current_platform_api: PlatformAPI = Depends(_get_platform_api),
    ) -> JSONResponse:
        try:
            approval = await current_platform_api.decide_approval(
                approval_id,
                approved=body.decision == "approved",
                comment=body.comment,
            )
        except EntityNotFoundError as exc:
            raise _api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=str(exc),
            ) from exc
        except InvalidRunStateError as exc:
            raise _api_http_exception(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.INVALID_STATE_TRANSITION.value,
                message=str(exc),
            ) from exc
        except ErrorCodeContractError as exc:
            if exc.error_code in {ErrorCode.APPROVAL_ALREADY_RESOLVED, ErrorCode.APPROVAL_EXPIRED}:
                raise _api_http_exception(
                    status_code=status.HTTP_409_CONFLICT,
                    code=exc.error_code.value,
                    message=str(exc),
                ) from exc
            raise
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=approval.to_dict())

    app.include_router(router)
    return app


def _get_platform_api(request: Request) -> PlatformAPI:
    platform_api = getattr(request.app.state, "platform_api", None)
    if platform_api is None:
        raise RuntimeError("Platform API is not configured")
    return cast(PlatformAPI, platform_api)


def _status_code_for_health(health: HealthCheckResult) -> int:
    return (
        status.HTTP_200_OK
        if health.status == "ready"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )


def _api_http_exception(*, status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _validate_workspace_path(workspace_path: str, *, config: APIConfig) -> str:
    raw_path = Path(workspace_path).expanduser()
    if not workspace_path.strip():
        raise _api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message="Workspace path must not be empty.",
        )
    try:
        resolved_path = raw_path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message=f"Workspace path is invalid: {workspace_path}",
        ) from exc
    if not raw_path.exists():
        raise _api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message=f"Workspace path does not exist: {resolved_path}",
        )
    if not raw_path.is_dir():
        raise _api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message=f"Workspace path must be a directory: {resolved_path}",
        )
    if raw_path.is_symlink():
        raise _api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message=f"Workspace path resolves through a symlink: {workspace_path}",
        )
    if config.allowed_workspace_roots:
        resolved_roots = tuple(Path(root).expanduser().resolve(strict=False) for root in config.allowed_workspace_roots)
        if not any(_is_within_root(resolved_path, root) for root in resolved_roots):
            raise _api_http_exception(
                status_code=status.HTTP_403_FORBIDDEN,
                code="workspace_not_allowed",
                message=f"Workspace path is outside allowed roots: {resolved_path}",
            )
    return str(resolved_path)


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _workspace_http_exception(exc: ErrorCodeContractError) -> HTTPException:
    if exc.error_code in {
        ErrorCode.WORKSPACE_NOT_FOUND,
        ErrorCode.WORKSPACE_PATH_INVALID,
        ErrorCode.WORKSPACE_SYMLINK_ESCAPE,
    }:
        return _api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message=str(exc),
        )
    if exc.error_code == ErrorCode.PERMISSION_DENIED:
        return _api_http_exception(
            status_code=status.HTTP_403_FORBIDDEN,
            code="forbidden",
            message=str(exc),
        )
    raise exc


def _format_sse_event(event: RunEvent) -> str:
    payload = json.dumps(event.to_dict(), separators=(",", ":"))
    return f"id: {event.event_id}\nevent: {event.event_type.value}\ndata: {payload}\n\n"


def _artifact_detail_from_model(artifact: Artifact) -> ArtifactDetailResponse:
    return ArtifactDetailResponse(
        artifact_id=str(artifact.artifact_id),
        run_id=str(artifact.run_id),
        artifact_type=artifact.artifact_type.value,
        label=artifact.label,
        uri=artifact.uri,
        created_at=artifact.created_at.isoformat().replace("+00:00", "Z"),
        content=None,
        content_inline=False,
        content_kind=None,
        content_note="Artifact content is not inlined by the local runtime store.",
    )


_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.CANCELLED,
        RunStatus.FAILED,
        RunStatus.SUCCEEDED,
    }
)


class _LocalPlatformAPI(PlatformAPI):
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
        return tuple(_artifact_detail_from_model(artifact) for artifact in artifacts)

    async def get_artifact(self, artifact_id: str) -> ArtifactDetailResponse | None:
        artifact = await self._require_repository().get_artifact(artifact_id)
        if artifact is None:
            return None
        return _artifact_detail_from_model(artifact)

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
    """Create the platform API from injected services."""
    return _LocalPlatformAPI(
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
    return _LocalPlatformAPI(
        agent_core=stack.agent_core,
        execution_runtime=stack.execution_runtime,
        repo_intelligence=repo_intelligence,
        observability=observability,
        coordinator=stack.coordinator,
    )
