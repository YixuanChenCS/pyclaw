from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Sequence

from packages.shared_types import (
    ApprovalDecision,
    FailureRecord,
    HealthCheckResult,
    MetricPoint,
    RepoContextResult,
    RunEvent,
    RunResult,
    RunStatus,
    TraceSpan,
    Workspace,
    WorkspaceRef,
)
from services.execution_runtime import SQLiteExecutionRuntimeRepository
from services.ops_observability import OpsObservabilityService

_TERMINAL_RUN_STATUSES = {
    RunStatus.CANCELLED,
    RunStatus.FAILED,
    RunStatus.SUCCEEDED,
}


@dataclass(slots=True)
class WorkspaceRegistryRepoStore:
    workspaces: dict[str, WorkspaceRef] = field(default_factory=dict)

    def register_workspace(self, workspace: WorkspaceRef) -> None:
        self.workspaces[str(workspace.workspace_id)] = workspace

    async def get_workspace(self, workspace_id):
        return self.workspaces.get(str(workspace_id))


class NoopObservabilityService(OpsObservabilityService):
    async def publish_event(self, event: RunEvent) -> None:
        return None

    async def record_metric(self, metric: MetricPoint) -> None:
        return None

    async def record_failure(self, failure: FailureRecord) -> None:
        return None

    async def start_trace(self, span: TraceSpan) -> str:
        return "noop-trace"

    async def finish_trace(self, trace_id: str, status: str) -> None:
        return None

    async def record_run_result(self, result: RunResult) -> None:
        return None

    async def get_health(self) -> HealthCheckResult:
        return HealthCheckResult(service="noop-observability", status="ready")


async def wait_for_run_result(
    repository: SQLiteExecutionRuntimeRepository,
    run_id: str,
    *,
    poll_interval: float = 0.05,
) -> RunResult:
    while True:
        result = await synthesize_run_result(repository, run_id)
        if result is None:
            raise ValueError(f"Run {run_id} was not found")
        if result.status in _TERMINAL_RUN_STATUSES:
            return result
        await asyncio.sleep(poll_interval)


async def synthesize_run_result(
    repository: SQLiteExecutionRuntimeRepository,
    run_id: str,
) -> RunResult | None:
    run = await repository.get_run(run_id)
    if run is None:
        return None

    artifacts = await repository.list_artifacts(run.run_id)
    session = await repository.load_agent_session(run.run_id)
    summary = None
    error_message = None
    if session is not None:
        summary = _summary_from_session(session.repo_context, session.current_plan, session.action_history)
        if run.status == RunStatus.FAILED and session.failure_history:
            error_message = session.failure_history[-1].message

    return RunResult(
        run_id=run.run_id,
        status=run.status,
        summary=summary,
        artifacts=tuple(artifacts),
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_message=error_message,
    )


def _summary_from_session(
    repo_context: RepoContextResult | None,
    current_plan,
    action_history: Sequence,
) -> str | None:
    if action_history:
        last_action = action_history[-1]
        summary_text = getattr(last_action, "summary_text", None)
        if summary_text:
            return summary_text
        reason = getattr(last_action, "reason", None)
        if reason:
            return str(reason)
    if current_plan is not None:
        if current_plan.summary:
            return current_plan.summary
        return current_plan.goal
    if repo_context is not None and repo_context.dependency_hints:
        return f"Context built for {len(repo_context.dependency_hints)} key files"
    return None
