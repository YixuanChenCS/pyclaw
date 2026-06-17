from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from apps.api.auth import require_permission
from packages.shared_types import (
    EntityNotFoundError,
    ErrorCode,
    ErrorCodeContractError,
    InvalidRunStateError,
    RunStatus,
)

from apps.api.errors import api_http_exception, contract_http_exception, error_response_doc
from apps.api.platform_api import PlatformAPI
from apps.api.routes._shared import (
    forbidden_response,
    get_api_config,
    get_platform_api,
    route_responses,
    run_creation_http_exception,
    unauthorized_response,
    validate_workspace_path,
    validation_error_response,
)
from apps.api.schemas import (
    RecoveryStatusResponse,
    RunAcceptedResponse,
    RunCreateRequestBody,
    RunResultResponse,
    RunSummaryResponse,
)


_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.CANCELLED,
        RunStatus.FAILED,
        RunStatus.SUCCEEDED,
    }
)


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/runs",
        tags=["runs"],
        summary="List runs",
        response_model=list[RunResultResponse],
        dependencies=[Depends(require_permission("runs:read"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("runs:read"),
            validation_error_response(),
        ),
    )
    async def list_runs(
        workspace_id: str | None = None,
        session_id: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int | None = Query(default=None, ge=1),
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        runs = await current_platform_api.list_runs(
            workspace_id=workspace_id,
            session_id=session_id,
            status=status_filter,
            limit=limit,
        )
        return JSONResponse(status_code=status.HTTP_200_OK, content=[run.to_dict() for run in runs])

    @router.post(
        "/runs",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["runs"],
        summary="Create a run",
        response_model=RunAcceptedResponse,
        dependencies=[Depends(require_permission("runs:create"))],
        responses=route_responses(
            unauthorized_response(),
            (
                status.HTTP_403_FORBIDDEN,
                {
                    "description": "The caller either lacks permission to create runs or requested a workspace outside allowed roots.",
                    "content": {
                        "application/json": {
                            "examples": {
                                "forbidden": {
                                    "summary": "Missing permission",
                                    "value": {
                                        "error": {
                                            "code": "forbidden",
                                            "message": "Permission denied. Missing required permission: runs:create",
                                        }
                                    },
                                },
                                "workspace_not_allowed": {
                                    "summary": "Workspace not allowed",
                                    "value": {
                                        "error": {
                                            "code": "workspace_not_allowed",
                                            "message": "Workspace path is outside allowed roots: /restricted/repo",
                                        }
                                    },
                                },
                            }
                        }
                    },
                },
            ),
            (
                status.HTTP_409_CONFLICT,
                error_response_doc(
                    description="The provided run id is already associated with a different request.",
                    code=ErrorCode.INVALID_REQUEST.value,
                    message="Run id run_conflict is already used by a different request.",
                ),
            ),
            validation_error_response(),
        ),
    )
    async def create_run(
        body: RunCreateRequestBody,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
        api_config=Depends(get_api_config),
    ) -> RunAcceptedResponse:
        if body.workspace_path is not None:
            canonical_workspace_path = validate_workspace_path(body.workspace_path, config=api_config)
            try:
                run_id = await current_platform_api.create_run_from_workspace(
                    workspace_path=canonical_workspace_path,
                    prompt=body.prompt,
                    target_paths=tuple(body.target_paths),
                    run_id=body.run_id,
                    session_id=body.session_id,
                )
            except ErrorCodeContractError as exc:
                raise run_creation_http_exception(exc) from exc
        else:
            try:
                run_id = await current_platform_api.create_run(body.to_run_request())
            except ErrorCodeContractError as exc:
                raise run_creation_http_exception(exc) from exc
        return RunAcceptedResponse(run_id=run_id, status=RunStatus.QUEUED.value)

    @router.get(
        "/runs/{run_id}",
        tags=["runs"],
        summary="Get a run",
        response_model=RunResultResponse,
        dependencies=[Depends(require_permission("runs:read"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("runs:read"),
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
    async def get_run(
        run_id: str,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        run = await current_platform_api.get_run(run_id)
        if run is None:
            raise api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=f"run not found: {run_id}",
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=run.to_dict())

    @router.get(
        "/runs/{run_id}/summary",
        tags=["runs"],
        summary="Get run summary",
        response_model=RunSummaryResponse,
        dependencies=[Depends(require_permission("runs:read"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("runs:read"),
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
    async def get_run_summary(
        run_id: str,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> RunSummaryResponse:
        run = await current_platform_api.get_run_summary(run_id)
        if run is None:
            raise api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=f"run not found: {run_id}",
            )
        completed = run.status in _TERMINAL_RUN_STATUSES
        return RunSummaryResponse(
            run_id=str(run.run_id),
            status=run.status.value,
            summary=run.summary if completed else None,
            completed=completed,
        )

    @router.post(
        "/runs/{run_id}/cancel",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["runs"],
        summary="Cancel a run",
        response_model=RunAcceptedResponse,
        dependencies=[Depends(require_permission("runs:cancel"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("runs:cancel"),
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
                    description="The run is already terminal and cannot be cancelled.",
                    code=ErrorCode.INVALID_STATE_TRANSITION.value,
                    message="Cannot cancel terminal run run_done",
                ),
            ),
        ),
    )
    async def cancel_run(
        run_id: str,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> RunAcceptedResponse:
        try:
            await current_platform_api.cancel_run(run_id)
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
        return RunAcceptedResponse(run_id=run_id, status=RunStatus.CANCELLING.value)

    @router.get(
        "/runs/{run_id}/recovery",
        tags=["runs"],
        summary="Get run recovery status",
        response_model=RecoveryStatusResponse,
        dependencies=[Depends(require_permission("runs:read"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("runs:read"),
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
                    description="The run does not currently require recovery.",
                    code=ErrorCode.INVALID_STATE_TRANSITION.value,
                    message="Run run_alpha is not waiting on recovery.",
                ),
            ),
        ),
    )
    async def get_run_recovery(
        run_id: str,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        try:
            recovery = await current_platform_api.get_recovery_status(run_id)
        except EntityNotFoundError as exc:
            raise api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=str(exc),
            ) from exc
        if recovery is None:
            raise api_http_exception(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.INVALID_STATE_TRANSITION.value,
                message=f"Run {run_id} is not waiting on recovery.",
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=recovery.to_dict())

    @router.post(
        "/runs/{run_id}/recovery/rollback",
        tags=["runs"],
        summary="Rollback a recoverable run task",
        response_model=RecoveryStatusResponse,
        dependencies=[Depends(require_permission("runs:recover"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("runs:recover"),
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
                    description="The run cannot be rolled back in its current state.",
                    code=ErrorCode.INVALID_STATE_TRANSITION.value,
                    message="No rollback snapshot exists for task task_patch",
                ),
            ),
            validation_error_response(),
        ),
    )
    async def rollback_run_recovery(
        run_id: str,
        task_id: str = Query(min_length=1),
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        try:
            recovery = await current_platform_api.rollback_recovery(run_id, task_id)
        except EntityNotFoundError as exc:
            raise api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=str(exc),
            ) from exc
        except ErrorCodeContractError as exc:
            raise contract_http_exception(exc) from exc
        except InvalidRunStateError as exc:
            raise api_http_exception(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.INVALID_STATE_TRANSITION.value,
                message=str(exc),
            ) from exc
        return JSONResponse(status_code=status.HTTP_200_OK, content=recovery.to_dict())

    return router


__all__ = ["build_router"]
