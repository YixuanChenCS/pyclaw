from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from packages.provider_adapters import DeploymentAdapter
from services.agent_core import LocalAgentRunnerConfig, build_local_agent_runner_stack, resolve_local_agent_runner_config
from services.repo_intelligence import LocalRepoIntelligenceService

from apps._local_support import NoopObservabilityService, WorkspaceRegistryRepoStore
from apps.api.errors import register_error_handlers
from apps.api.operability import install_operability
from apps.api.platform_api import (
    LocalPlatformAPIAdapter,
    PlatformAPI,
    create_platform_api,
)
from apps.api.routes import build_api_router
from apps.api.schemas import (
    APIConfig,
    APIAuthPrincipal,
    ApprovalDecisionRequestBody,
    ApprovalRecordResponse,
    ArtifactDetailResponse,
    ArtifactSummaryResponse,
    DeploymentCreateRequestBody,
    DeploymentResultResponse,
    HealthResponse,
    RecoveryStatusResponse,
    RunAcceptedResponse,
    RunCreateRequestBody,
    RunEventResponse,
    RunResultResponse,
    RunSummaryResponse,
)


_OPENAPI_TAGS = [
    {"name": "health", "description": "Platform and dependency readiness checks."},
    {"name": "runs", "description": "Run creation, listing, state inspection, and cancellation."},
    {"name": "events", "description": "Run event replay and live event streaming."},
    {"name": "approvals", "description": "Human approval inspection and approval decisions."},
    {"name": "artifacts", "description": "Run artifact listing and artifact retrieval."},
    {"name": "deployments", "description": "Deployment trigger requests for completed runs."},
]

_API_DESCRIPTION = (
    "Thin HTTP control plane for Pyclaw runs, approvals, artifacts, and deployments. "
    "POST /runs enqueues work in execution_runtime, but this API process does not start a background worker loop to claim and execute queued runs."
)


def create_app(
    *,
    platform_api: PlatformAPI,
    config: APIConfig | None = None,
    title: str = "Pyclaw API",
) -> FastAPI:
    """Create a thin FastAPI application shell over the platform API."""

    api_config = config or APIConfig()
    app = FastAPI(
        title=title,
        description=_API_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
    )
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

    register_error_handlers(app)
    install_operability(app, config=api_config)
    app.include_router(build_api_router())
    return app


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
    "APIConfig",
    "APIAuthPrincipal",
    "ApprovalDecisionRequestBody",
    "ApprovalRecordResponse",
    "ArtifactDetailResponse",
    "ArtifactSummaryResponse",
    "DeploymentCreateRequestBody",
    "DeploymentResultResponse",
    "HealthResponse",
    "PlatformAPI",
    "RecoveryStatusResponse",
    "RunAcceptedResponse",
    "RunCreateRequestBody",
    "RunEventResponse",
    "RunResultResponse",
    "RunSummaryResponse",
    "build_local_agent_runner_stack",
    "create_app",
    "create_local_platform_api_from_config",
    "create_local_platform_api_from_env",
    "create_platform_api",
    "resolve_local_agent_runner_config",
]
