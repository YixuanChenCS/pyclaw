from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from apps.api.auth import require_permission
from packages.shared_types import EntityNotFoundError, ErrorCode, ErrorCodeContractError, InvalidRunStateError

from apps.api.errors import api_http_exception, contract_http_exception, error_response_doc
from apps.api.platform_api import PlatformAPI
from apps.api.routes._shared import (
    forbidden_response,
    get_platform_api,
    route_responses,
    unauthorized_response,
    validation_error_response,
)
from apps.api.schemas import DeploymentCreateRequestBody, DeploymentResultResponse


def build_router() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/deployments",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["deployments"],
        summary="Trigger a deployment",
        response_model=DeploymentResultResponse,
        dependencies=[Depends(require_permission("deployments:create"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("deployments:create"),
            (
                status.HTTP_404_NOT_FOUND,
                error_response_doc(
                    description="The requested run does not exist.",
                    code=ErrorCode.NOT_FOUND.value,
                    message="run not found: run_missing",
                ),
            ),
            (
                status.HTTP_409_CONFLICT,
                error_response_doc(
                    description="The run is not in a deployable terminal state.",
                    code=ErrorCode.INVALID_STATE_TRANSITION.value,
                    message="Cannot deploy run run_alpha in status running",
                ),
            ),
            (
                status.HTTP_503_SERVICE_UNAVAILABLE,
                error_response_doc(
                    description="No deployment adapter is configured for this API instance.",
                    code=ErrorCode.DEPLOYMENT_UNAVAILABLE.value,
                    message="No deployment adapter is configured.",
                ),
            ),
            validation_error_response(),
        ),
    )
    async def trigger_deployment(
        body: DeploymentCreateRequestBody,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        try:
            result = await current_platform_api.trigger_deployment(body.to_deployment_request())
        except EntityNotFoundError as exc:
            raise api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=str(exc),
            ) from exc
        except InvalidRunStateError as exc:
            raise api_http_exception(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.INVALID_STATE_TRANSITION.value,
                message=str(exc),
            ) from exc
        except ErrorCodeContractError as exc:
            raise contract_http_exception(exc) from exc
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=result.to_dict())

    return router


__all__ = ["build_router"]
