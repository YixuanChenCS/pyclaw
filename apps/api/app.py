from __future__ import annotations

from typing import AsyncIterator, Sequence

from packages.provider_adapters import DeploymentAdapter
from packages.shared_types import (
    ApprovalDecision,
    ApprovalRequest,
    DeploymentRequest,
    DeploymentResult,
    EntityNotFoundError,
    ErrorCode,
    ErrorCodeContractError,
    HealthCheckResult,
    RunEvent,
    RunRequest,
    RunResult,
    RunStatus,
)
from services.agent_core import (
    AgentCoreCoordinator,
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

    async def list_runs(
        self,
        workspace_id: str | None = None,
        *,
        session_id: str | None = None,
        status: RunStatus | str | None = None,
    ) -> Sequence[RunResult]:
        raise NotImplementedError

    async def get_run(self, run_id: str) -> RunResult | None:
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
        coordinator: AgentCoreCoordinator | None = None,
    ) -> None:
        self._agent_core = agent_core
        self._execution_runtime = execution_runtime
        self._repo_intelligence = repo_intelligence
        self._observability = observability
        self._coordinator = coordinator or AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=execution_runtime,
            session_store=getattr(execution_runtime, "repository", None),
            repo_intelligence=repo_intelligence,
            repo_store=getattr(execution_runtime, "_repo_store", None),
        )

    async def create_run(self, request: RunRequest) -> str:
        return await self._execution_runtime.enqueue_run(request)

    async def list_runs(
        self,
        workspace_id: str | None = None,
        *,
        session_id: str | None = None,
        status: RunStatus | str | None = None,
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
        return tuple(results)

    async def get_run(self, run_id: str) -> RunResult | None:
        return await synthesize_run_result(self._require_repository(), run_id)

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
