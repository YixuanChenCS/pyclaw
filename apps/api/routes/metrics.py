from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from apps.api.operability import APIMetrics, render_runtime_prometheus
from apps.api.platform_api import PlatformAPI
from apps.api.routes._shared import get_platform_api


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/metrics",
        tags=["health"],
        summary="Get scrapeable API metrics",
        response_class=PlainTextResponse,
        responses={
            200: {
                "description": "Prometheus text exposition for API request metrics.",
                "content": {
                    "text/plain": {
                        "schema": {"type": "string"},
                    }
                },
            }
        },
    )
    async def get_metrics(request: Request) -> PlainTextResponse:
        metrics = getattr(request.app.state, "api_metrics", None)
        if not isinstance(metrics, APIMetrics):
            body = "# API metrics are unavailable.\n"
        else:
            body = metrics.render_prometheus()
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    @router.get(
        "/metrics/runtime",
        tags=["health"],
        summary="Get scrapeable runtime metrics",
        response_class=PlainTextResponse,
        responses={
            200: {
                "description": "Prometheus text exposition for execution runtime gauges.",
                "content": {
                    "text/plain": {
                        "schema": {"type": "string"},
                    }
                },
            }
        },
    )
    async def get_runtime_metrics(
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> PlainTextResponse:
        snapshot = await current_platform_api.get_operability_snapshot()
        body = render_runtime_prometheus(snapshot)
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    return router


__all__ = ["build_router"]
