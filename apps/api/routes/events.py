from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from apps.api.auth import require_permission
from packages.shared_types import EntityNotFoundError, ErrorCode, ErrorCodeContractError

from apps.api.errors import api_http_exception, contract_http_exception, error_response_doc
from apps.api.platform_api import PlatformAPI
from apps.api.routes._shared import (
    format_sse_event,
    forbidden_response,
    get_platform_api,
    route_responses,
    unauthorized_response,
)
from apps.api.schemas import RunEventResponse


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/runs/{run_id}/events",
        tags=["events"],
        summary="Replay persisted run events",
        response_model=list[RunEventResponse],
        dependencies=[Depends(require_permission("events:read"))],
        responses=route_responses(
            unauthorized_response(),
            forbidden_response("events:read"),
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
    async def list_run_events(
        run_id: str,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> JSONResponse:
        try:
            events = await current_platform_api.list_run_events(run_id)
        except EntityNotFoundError as exc:
            raise api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=str(exc),
            ) from exc
        return JSONResponse(status_code=status.HTTP_200_OK, content=[event.to_dict() for event in events])

    @router.get(
        "/runs/{run_id}/events/stream",
        tags=["events"],
        summary="Stream live run events",
        response_class=StreamingResponse,
        dependencies=[Depends(require_permission("events:read"))],
        responses=route_responses(
            (
                status.HTTP_200_OK,
                {
                    "description": "Server-sent event stream of run events.",
                    "content": {
                        "text/event-stream": {
                            "schema": {"type": "string"},
                        }
                    },
                },
            ),
            unauthorized_response(),
            forbidden_response("events:read"),
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
                    description="The requested replay checkpoint is no longer available.",
                    code=ErrorCode.EVENT_REPLAY_GAP.value,
                    message="Last event id was not found for run run_alpha: 999",
                ),
            ),
        ),
    )
    async def stream_run_events(
        run_id: str,
        request: Request,
        last_event_id: str | None = None,
        current_platform_api: PlatformAPI = Depends(get_platform_api),
    ) -> StreamingResponse:
        if await current_platform_api.get_run(run_id) is None:
            raise api_http_exception(
                status_code=status.HTTP_404_NOT_FOUND,
                code=ErrorCode.NOT_FOUND.value,
                message=f"run not found: {run_id}",
            )

        stream = current_platform_api.stream_run_events(run_id, last_event_id=last_event_id)
        first_event = None
        try:
            first_event = await anext(stream)
        except StopAsyncIteration:
            first_event = None
        except ErrorCodeContractError as exc:
            raise contract_http_exception(exc) from exc

        async def event_generator():
            if first_event is not None:
                yield format_sse_event(first_event)
            async for event in stream:
                if await request.is_disconnected():
                    break
                yield format_sse_event(event)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return router


__all__ = ["build_router"]
