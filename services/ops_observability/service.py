from __future__ import annotations

from abc import ABC, abstractmethod

from packages.shared_types import (
    FailureRecord,
    HealthCheckResult,
    MetricPoint,
    RunEvent,
    RunResult,
    TraceSpan,
)


class OpsObservabilityService(ABC):
    """Telemetry, audit, and health service."""

    @abstractmethod
    async def publish_event(self, event: RunEvent) -> None:
        """Publish a run event to subscribers and durable storage."""

    @abstractmethod
    async def record_metric(self, metric: MetricPoint) -> None:
        """Record a metric data point."""

    @abstractmethod
    async def record_failure(self, failure: FailureRecord) -> None:
        """Record a failure for alerting and debugging."""

    @abstractmethod
    async def start_trace(self, span: TraceSpan) -> str:
        """Start a trace or span and return its identifier."""

    @abstractmethod
    async def finish_trace(self, trace_id: str, status: str) -> None:
        """Finish a trace or span."""

    @abstractmethod
    async def record_run_result(self, result: RunResult) -> None:
        """Record the terminal result of a run."""

    @abstractmethod
    async def get_health(self) -> HealthCheckResult:
        """Return health and readiness information."""
