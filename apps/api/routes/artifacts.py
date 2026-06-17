from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse, JSONResponse

from apps.api.auth import require_permission
from packages.shared_types import EntityNotFoundError, ErrorCode, ErrorCodeContractError

from apps.api.errors import api_http_exception, contract_http_exception, error_response_doc
from apps.api.platform_api import PlatformAPI
from apps.api.routes._shared import forbidden_response, get_platform_api, route_responses, unauthorized_response
from apps.api.schemas import ArtifactDetailResponse


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/runs/{run_id}/artifacts",
        tags=["artifacts"],
        summary="List run artifacts",
        response_model=list[ArtifactDetailResponse],
        dependencies=[Depends(require_permission("artifacts:read"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("artifacts:read"),
            (
                status.HTTP_404_NOT_FOUND,
                error_response_doc(
                    description="The requested run does not exist.",
                    code=ErrorCode.NOT_FOUND.value,
                    message="run not found: run_missing",
                ),
            ),
        ),
    )
    async def list_run_artifacts(
        run_id: str,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        try:
            artifacts = await current_platform_api.list_artifacts(run_id)
        except EntityNotFoundError as exc:
            raise api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=str(exc),
            ) from exc
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=[artifact.model_dump(mode="json") for artifact in artifacts],
        )

    @router.get(
        "/artifacts/{artifact_id}",
        tags=["artifacts"],
        summary="Get an artifact",
        response_model=ArtifactDetailResponse,
        dependencies=[Depends(require_permission("artifacts:read"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("artifacts:read"),
            (
                status.HTTP_404_NOT_FOUND,
                error_response_doc(
                    description="The requested artifact does not exist.",
                    code=ErrorCode.NOT_FOUND.value,
                    message="artifact not found: artifact_missing",
                ),
            ),
        ),
    )
    async def get_artifact(
        artifact_id: str,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        artifact = await current_platform_api.get_artifact(artifact_id)
        if artifact is None:
            raise api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=f"artifact not found: {artifact_id}",
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=artifact.model_dump(mode="json"))

    @router.get(
        "/artifacts/{artifact_id}/download",
        tags=["artifacts"],
        summary="Download an artifact file",
        dependencies=[Depends(require_permission("artifacts:read"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("artifacts:read"),
            (
                status.HTTP_400_BAD_REQUEST,
                error_response_doc(
                    description="The artifact exists, but it does not expose a downloadable file.",
                    code=ErrorCode.INVALID_REQUEST.value,
                    message="Artifact artifact_inline does not expose a downloadable file.",
                ),
            ),
            (
                status.HTTP_404_NOT_FOUND,
                error_response_doc(
                    description="The requested artifact does not exist.",
                    code=ErrorCode.NOT_FOUND.value,
                    message="artifact not found: artifact_missing",
                ),
            ),
        ),
    )
    async def download_artifact(
        artifact_id: str,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> FileResponse:
        try:
            download = await current_platform_api.get_artifact_download(artifact_id)
        except ErrorCodeContractError as exc:
            raise contract_http_exception(exc) from exc
        if download is None:
            raise api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=f"artifact not found: {artifact_id}",
            )
        return FileResponse(
            path=download.path,
            media_type=download.media_type,
            filename=download.filename,
        )

    return router


__all__ = ["build_router"]
