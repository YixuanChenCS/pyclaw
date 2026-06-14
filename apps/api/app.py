from __future__ import annotations

from typing import AsyncIterator, Sequence

from packages.shared_types import (
    ApprovalDecision,
    ApprovalRequest,
    DeploymentRequest,
    DeploymentResult,
    HealthCheckResult,
    RunEvent,
    RunRequest,
    RunResult,
)
from services.agent_core import (
    LocalAgentRunnerConfig,
    build_local_agent_runner_stack,
    resolve_local_agent_runner_config,
)
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

    async def list_runs(self, workspace_id: str | None = None) -> Sequence[RunResult]:
        raise NotImplementedError

    async def get_run(self, run_id: str) -> RunResult | None:
        raise NotImplementedError

    async def stream_run_events(self, run_id: str) -> AsyncIterator[RunEvent]:
        raise NotImplementedError

    async def create_approval_request(self, request: ApprovalRequest) -> str:
        raise NotImplementedError

    async def submit_approval(self, decision: ApprovalDecision) -> None:
        raise NotImplementedError

    async def trigger_deployment(self, request: DeploymentRequest) -> DeploymentResult:
        raise NotImplementedError

    async def get_health(self) -> HealthCheckResult:
        raise NotImplementedError


class _LocalPlatformAPI(PlatformAPI):
    def __init__(
        self,
        *,
        agent_core,
        execution_runtime: ExecutionRuntimeService,
        repo_intelligence: RepoIntelligenceService,
        observability: OpsObservabilityService,
    ) -> None:
        self._agent_core = agent_core
        self._execution_runtime = execution_runtime
        self._repo_intelligence = repo_intelligence
        self._observability = observability

    async def create_run(self, request: RunRequest) -> str:
        return await self._execution_runtime.enqueue_run(request)

    async def list_runs(self, workspace_id: str | None = None) -> Sequence[RunResult]:
        repository = self._require_repository()
        runs = await repository.list_runs(workspace_id)
        results: list[RunResult] = []
        for run in runs:
            result = await synthesize_run_result(repository, str(run.run_id))
            if result is not None:
                results.append(result)
        return tuple(results)

    async def get_run(self, run_id: str) -> RunResult | None:
        return await synthesize_run_result(self._require_repository(), run_id)

    async def stream_run_events(self, run_id: str) -> AsyncIterator[RunEvent]:
        async for event in self._execution_runtime.stream_events(run_id):
            yield event

    async def create_approval_request(self, request: ApprovalRequest) -> str:
        return await self._execution_runtime.request_approval(str(request.run_id), request)

    async def submit_approval(self, decision: ApprovalDecision) -> None:
        record = getattr(self._execution_runtime, "record_approval_decision", None)
        if not callable(record):
            raise NotImplementedError("Execution runtime does not support approval decisions")
        await record(decision)

    async def trigger_deployment(self, request: DeploymentRequest) -> DeploymentResult:
        return await self._execution_runtime.deploy(request)

    async def get_health(self) -> HealthCheckResult:
        return await self._observability.get_health()

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
) -> PlatformAPI:
    """Create the platform API from injected services."""
    return _LocalPlatformAPI(
        agent_core=agent_core,
        execution_runtime=execution_runtime,
        repo_intelligence=repo_intelligence,
        observability=observability,
    )


def create_local_platform_api_from_env() -> PlatformAPI:
    config = resolve_local_agent_runner_config(workspace_root=".")
    return create_local_platform_api_from_config(config)


def create_local_platform_api_from_config(
    config: LocalAgentRunnerConfig,
) -> PlatformAPI:
    workspace_store = WorkspaceRegistryRepoStore()
    repo_intelligence = LocalRepoIntelligenceService()
    observability = NoopObservabilityService()
    stack = build_local_agent_runner_stack(
        config=config,
        repo_store=workspace_store,
        repo_intelligence=repo_intelligence,
    )
    return _LocalPlatformAPI(
        agent_core=stack.agent_core,
        execution_runtime=stack.execution_runtime,
        repo_intelligence=repo_intelligence,
        observability=observability,
    )
