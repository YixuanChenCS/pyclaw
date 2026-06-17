from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from apps.api.auth import require_permission
from packages.shared_types import ApprovalRecord, EntityNotFoundError, ErrorCode, ErrorCodeContractError, InvalidRunStateError

from apps.api.errors import api_http_exception, contract_http_exception, error_response_doc
from apps.api.platform_api import PlatformAPI
from apps.api.routes._shared import (
    forbidden_response,
    get_platform_api,
    route_responses,
    unauthorized_response,
    validation_error_response,
)
from apps.api.schemas import ApprovalDecisionRequestBody, ApprovalRecordResponse


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/approvals",
        tags=["approvals"],
        summary="List approvals",
        response_model=list[ApprovalRecordResponse],
        dependencies=[Depends(require_permission("approvals:read"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("approvals:read"),
        ),
    )
    async def list_approvals(
        run_id: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        approvals = await current_platform_api.list_approvals(run_id=run_id, status=status_filter)
        return JSONResponse(status_code=status.HTTP_200_OK, content=[approval.to_dict() for approval in approvals])

    @router.get(
        "/approvals/{approval_id}",
        tags=["approvals"],
        summary="Get an approval",
        response_model=ApprovalRecordResponse,
        dependencies=[Depends(require_permission("approvals:read"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("approvals:read"),
            (
                status.HTTP_404_NOT_FOUND,
                error_response_doc(
                    description="The requested approval does not exist.",
                    code=ErrorCode.NOT_FOUND.value,
                    message="approval not found: approval_missing",
                ),
            ),
        ),
    )
    async def get_approval(
        approval_id: str,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        approval = await current_platform_api.get_approval(approval_id)
        if approval is None:
            raise api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=f"approval not found: {approval_id}",
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=approval.to_dict())

    @router.post(
        "/approvals/{approval_id}/decision",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["approvals"],
        summary="Submit an approval decision",
        response_model=ApprovalRecordResponse,
        dependencies=[Depends(require_permission("approvals:decide"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("approvals:decide"),
            (
                status.HTTP_404_NOT_FOUND,
                error_response_doc(
                    description="The requested approval does not exist.",
                    code=ErrorCode.NOT_FOUND.value,
                    message="approval not found: approval_missing",
                ),
            ),
            (
                status.HTTP_409_CONFLICT,
                error_response_doc(
                    description="The approval was already finalized.",
                    code=ErrorCode.APPROVAL_ALREADY_RESOLVED.value,
                    message="Approval approval_done is already finalized.",
                ),
            ),
            (
                status.HTTP_410_GONE,
                error_response_doc(
                    description="The approval expired before a decision was submitted.",
                    code=ErrorCode.APPROVAL_EXPIRED.value,
                    message="Approval approval_pending expired at 2026-06-15T00:00:00Z.",
                ),
            ),
            validation_error_response(),
        ),
    )
    @router.post(
        "/approvals/{approval_id}",
        status_code=status.HTTP_202_ACCEPTED,
        include_in_schema=False,
        response_model=ApprovalRecordResponse,
    )
    async def decide_approval(
        approval_id: str,
        body: ApprovalDecisionRequestBody,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        try:
            approval = await current_platform_api.decide_approval(
                approval_id,
                approved=body.decision == "approved",
                comment=body.comment,
            )
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
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=approval.to_dict())

    return router


__all__ = ["build_router"]
