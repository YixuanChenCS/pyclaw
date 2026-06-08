from __future__ import annotations

from typing import AsyncIterator, Protocol, Sequence

from .models import (
    Artifact,
    CommandRequest,
    CommandResult,
    FailureRecord,
    HealthCheckResult,
    LLMMessage,
    LLMResponse,
    MetricPoint,
    PatchProposal,
    RepoContextRequest,
    RepoContextResult,
    Run,
    RunEvent,
    RunResult,
    Session,
    Task,
    TraceSpan,
    Workspace,
)
from .ids import EventId, RunId, SessionId, WorkspaceId


class LLMProvider(Protocol):
    async def complete(self, messages: Sequence[LLMMessage], model: str) -> LLMResponse:
        """Return a single non-streaming completion."""

    async def stream(self, messages: Sequence[LLMMessage], model: str) -> AsyncIterator[str]:
        """Yield completion chunks in order."""

    async def count_tokens(self, messages: Sequence[LLMMessage], model: str) -> int:
        """Estimate the token cost of a request."""


class RepoStore(Protocol):
    async def get_workspace(self, workspace_id: WorkspaceId) -> Workspace | None:
        """Return the known workspace descriptor."""

    async def list_files(
        self,
        workspace_id: WorkspaceId,
        relative_paths: Sequence[str] = (),
    ) -> Sequence[str]:
        """List repo files, optionally scoped to target paths."""

    async def read_text(self, workspace_id: WorkspaceId, path: str) -> str:
        """Read a text file from the workspace."""

    async def current_revision(self, workspace_id: WorkspaceId) -> str | None:
        """Return the current git revision when available."""


class CodeIndexer(Protocol):
    async def build_context(self, request: RepoContextRequest) -> RepoContextResult:
        """Build stable repository context for a run."""

    async def refresh(self, workspace_id: WorkspaceId, changed_paths: Sequence[str]) -> None:
        """Refresh index state after file changes."""


class ExecutionBackend(Protocol):
    async def execute(self, request: CommandRequest) -> CommandResult:
        """Run a command inside the execution boundary."""

    async def cancel(self, run_id: RunId) -> None:
        """Cancel active work for a run when supported."""


class RunStore(Protocol):
    async def create_session(self, session: Session) -> None:
        """Persist a session record."""

    async def get_session(self, session_id: SessionId) -> Session | None:
        """Load a session by identifier."""

    async def create_run(self, run: Run) -> None:
        """Persist a new run record."""

    async def update_run(self, run: Run) -> None:
        """Persist the latest run state."""

    async def get_run(self, run_id: RunId) -> Run | None:
        """Load a run by identifier."""

    async def upsert_task(self, task: Task) -> None:
        """Persist or replace a task record."""

    async def list_tasks(self, run_id: RunId) -> Sequence[Task]:
        """Return tasks for a run."""

    async def create_artifact(self, artifact: Artifact) -> None:
        """Persist a new artifact record."""

    async def list_artifacts(self, run_id: RunId) -> Sequence[Artifact]:
        """Return artifacts for a run."""

    async def append_event(self, event: RunEvent) -> None:
        """Persist a durable run event."""

    async def list_events(self, run_id: RunId) -> Sequence[RunEvent]:
        """Return durable run events."""

    async def save_result(self, result: RunResult) -> None:
        """Persist the terminal run result."""

    async def get_result(self, run_id: RunId) -> RunResult | None:
        """Load the terminal run result when present."""


class EventBus(Protocol):
    async def publish(self, event: RunEvent) -> None:
        """Publish an event to live subscribers."""

    async def subscribe(
        self,
        run_id: RunId,
        *,
        after: EventId | None = None,
    ) -> AsyncIterator[RunEvent]:
        """Stream events for a run, optionally after a checkpoint."""


class PatchApplier(Protocol):
    async def apply(self, workspace_id: WorkspaceId, proposal: PatchProposal) -> Artifact:
        """Apply a proposed patch and return the resulting artifact."""


class TelemetrySink(Protocol):
    async def record_event(self, event: RunEvent) -> None:
        """Record an event for audit or analytics."""

    async def record_metric(self, metric: MetricPoint) -> None:
        """Record a metric data point."""

    async def record_failure(self, failure: FailureRecord) -> None:
        """Record a structured failure."""

    async def start_trace(self, span: TraceSpan) -> str:
        """Start a trace span and return its identifier."""

    async def finish_trace(self, trace_id: str, status: str) -> None:
        """Finish a trace span."""

    async def get_health(self) -> HealthCheckResult:
        """Return sink readiness."""


__all__ = [
    "CodeIndexer",
    "EventBus",
    "ExecutionBackend",
    "LLMProvider",
    "PatchApplier",
    "RepoStore",
    "RunStore",
    "TelemetrySink",
]
