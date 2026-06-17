from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from apps.api.platform_api import PlatformAPI
from apps.api.routes._shared import get_platform_api, status_code_for_health
from apps.api.schemas import HealthResponse


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/health",
        tags=["health"],
        summary="Get platform health",
        description="Returns component-level health for the platform API and its local dependencies.",
        response_model=HealthResponse,
    )
    async def get_health(
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        health = await current_platform_api.get_health()
        return JSONResponse(
            status_code=status_code_for_health(health),
            content=health.to_dict(),
        )

    return router


__all__ = ["build_router"]
