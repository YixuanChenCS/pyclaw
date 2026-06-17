from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request, status

from apps.api.auth import authenticate_request, is_public_request
from apps.api.errors import api_error_response
from apps.api.schemas import APIConfig


@dataclass(frozen=True, slots=True)
class RequestMetric:
    method: str
    route: str
    status_code: int


class APIMetrics:
    def __init__(self) -> None:
        self._requests_total = 0
        self._request_counts: Counter[RequestMetric] = Counter()
        self._duration_ms_total = 0.0

    def record(self, *, method: str, route: str, status_code: int, duration_ms: float) -> None:
        self._requests_total += 1
        self._request_counts[RequestMetric(method=method, route=route, status_code=status_code)] += 1
        self._duration_ms_total += duration_ms

    def snapshot(self) -> dict[str, object]:
        return {
            "requests_total": self._requests_total,
            "duration_ms_total": round(self._duration_ms_total, 3),
            "request_counts": [
                {
                    "method": item.method,
                    "route": item.route,
                    "status_code": item.status_code,
                    "count": count,
                }
                for item, count in sorted(
                    self._request_counts.items(),
                    key=lambda pair: (pair[0].route, pair[0].method, pair[0].status_code),
                )
            ],
        }

    def render_prometheus(self) -> str:
        lines = [
            "# HELP pyclaw_api_http_requests_total Total number of HTTP requests handled by the API.",
            "# TYPE pyclaw_api_http_requests_total counter",
            f"pyclaw_api_http_requests_total {self._requests_total}",
            "# HELP pyclaw_api_http_request_duration_ms_total Total HTTP request handling time in milliseconds.",
            "# TYPE pyclaw_api_http_request_duration_ms_total counter",
            f"pyclaw_api_http_request_duration_ms_total {round(self._duration_ms_total, 3)}",
            "# HELP pyclaw_api_http_requests_by_route_total Total HTTP requests grouped by method, route, and status code.",
            "# TYPE pyclaw_api_http_requests_by_route_total counter",
        ]
        for item, count in sorted(
            self._request_counts.items(),
            key=lambda pair: (pair[0].route, pair[0].method, pair[0].status_code),
        ):
            lines.append(
                "pyclaw_api_http_requests_by_route_total"
                f'{{method="{_escape_label_value(item.method)}",route="{_escape_label_value(item.route)}",status_code="{item.status_code}"}} {count}'
            )
        return "\n".join(lines) + "\n"


def install_operability(app: FastAPI, *, config: APIConfig) -> None:
    metrics = APIMetrics()
    access_logger = logging.getLogger("pyclaw.api.access")
    app.state.api_metrics = metrics

    @app.middleware("http")
    async def api_operability_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid4().hex
        request.state.request_id = request_id
        started_at = perf_counter()

        if is_public_request(request):
            request.state.auth_context = None
            response = await call_next(request)
        else:
            auth_context = authenticate_request(request, config)
            request.state.auth_context = auth_context
            if config.auth_enabled() and auth_context is None:
                response = api_error_response(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    code="unauthorized",
                    message="Missing or invalid bearer token.",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            else:
                response = await call_next(request)

        duration_ms = (perf_counter() - started_at) * 1000.0
        route = _resolve_route_template(request)
        metrics.record(
            method=request.method,
            route=route,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        response.headers.setdefault("X-Request-ID", request_id)
        access_logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "route": route,
                    "subject": getattr(getattr(request.state, "auth_context", None), "subject", None),
                    "status_code": response.status_code,
                    "duration_ms": round(duration_ms, 3),
                },
                separators=(",", ":"),
            )
        )
        return response


def render_runtime_prometheus(snapshot: dict[str, object]) -> str:
    runtime = snapshot.get("runtime", {})
    if not isinstance(runtime, dict):
        return ""
    lines = [
        "# HELP pyclaw_runtime_queue_depth Number of queued runs in execution runtime.",
        "# TYPE pyclaw_runtime_queue_depth gauge",
        f"pyclaw_runtime_queue_depth {_coerce_metric_value(runtime.get('queue_depth'))}",
        "# HELP pyclaw_runtime_stale_run_count Number of stale running runs with expired leases.",
        "# TYPE pyclaw_runtime_stale_run_count gauge",
        f"pyclaw_runtime_stale_run_count {_coerce_metric_value(runtime.get('stale_run_count'))}",
        "# HELP pyclaw_runtime_needs_recovery_count Number of runs currently waiting on manual recovery.",
        "# TYPE pyclaw_runtime_needs_recovery_count gauge",
        f"pyclaw_runtime_needs_recovery_count {_coerce_metric_value(runtime.get('needs_recovery_count'))}",
        "# HELP pyclaw_runtime_active_lease_count Number of active run leases currently held in runtime storage.",
        "# TYPE pyclaw_runtime_active_lease_count gauge",
        f"pyclaw_runtime_active_lease_count {_coerce_metric_value(runtime.get('active_lease_count'))}",
    ]
    queue = runtime.get("queue", {})
    if isinstance(queue, dict):
        lines.extend(
            [
                "# HELP pyclaw_runtime_runs_by_status Number of runs grouped by runtime status.",
                "# TYPE pyclaw_runtime_runs_by_status gauge",
            ]
        )
        for status_name, count in sorted(queue.items()):
            lines.append(
                "pyclaw_runtime_runs_by_status"
                f'{{status="{_escape_label_value(str(status_name))}"}} {_coerce_metric_value(count)}'
            )
    return "\n".join(lines) + "\n"


def _resolve_route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path or request.url.path)


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _coerce_metric_value(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return 0


__all__ = ["APIMetrics", "install_operability", "render_runtime_prometheus"]
