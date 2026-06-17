from __future__ import annotations

from fastapi import APIRouter

from .approvals import build_router as build_approvals_router
from .artifacts import build_router as build_artifacts_router
from .deployments import build_router as build_deployments_router
from .events import build_router as build_events_router
from .health import build_router as build_health_router
from .metrics import build_router as build_metrics_router
from .runs import build_router as build_runs_router


def build_api_router() -> APIRouter:
    router = APIRouter()
    router.include_router(build_health_router())
    router.include_router(build_metrics_router())
    router.include_router(build_runs_router())
    router.include_router(build_events_router())
    router.include_router(build_artifacts_router())
    router.include_router(build_approvals_router())
    router.include_router(build_deployments_router())
    return router


__all__ = ["build_api_router"]
