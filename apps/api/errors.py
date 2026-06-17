from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from packages.shared_types import (
    EntityNotFoundError,
    ErrorCode,
    ErrorCodeContractError,
    InvalidRunStateError,
)


class APIErrorBody(BaseModel):
    code: str
    message: str


class APIErrorResponse(BaseModel):
    error: APIErrorBody


def api_error_content(*, code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def api_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=api_error_content(code=code, message=message),
        headers=headers,
    )


def api_http_exception(*, status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def error_response_doc(
    *,
    description: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "model": APIErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "example": api_error_content(code=code, message=message),
            }
        },
    }


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and {"code", "message"} <= set(exc.detail):
            return api_error_response(
                status_code=exc.status_code,
                code=str(exc.detail["code"]),
                message=str(exc.detail["message"]),
                headers=exc.headers,
            )
        return api_error_response(
            status_code=exc.status_code,
            code=_default_error_code_for_status(exc.status_code),
            message=str(exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(
        _request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        return api_error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code=ErrorCode.INVALID_REQUEST.value,
            message="Request validation failed.",
        )

    @app.exception_handler(EntityNotFoundError)
    async def entity_not_found_exception_handler(
        _request: Request,
        exc: EntityNotFoundError,
    ) -> JSONResponse:
        return api_error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ErrorCode.NOT_FOUND.value,
            message=str(exc),
        )

    @app.exception_handler(InvalidRunStateError)
    async def invalid_run_state_exception_handler(
        _request: Request,
        exc: InvalidRunStateError,
    ) -> JSONResponse:
        return api_error_response(
            status_code=status.HTTP_409_CONFLICT,
            code=ErrorCode.INVALID_STATE_TRANSITION.value,
            message=str(exc),
        )

    @app.exception_handler(ErrorCodeContractError)
    async def contract_exception_handler(
        _request: Request,
        exc: ErrorCodeContractError,
    ) -> JSONResponse:
        mapped = contract_http_exception(exc)
        return api_error_response(
            status_code=mapped.status_code,
            code=str(mapped.detail["code"]),
            message=str(mapped.detail["message"]),
            headers=mapped.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_exception_handler(_request: Request, _exc: Exception) -> JSONResponse:
        return api_error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.UNKNOWN_ERROR.value,
            message="Internal server error.",
        )


def contract_http_exception(exc: ErrorCodeContractError) -> HTTPException:
    if exc.error_code in {
        ErrorCode.WORKSPACE_NOT_FOUND,
        ErrorCode.WORKSPACE_PATH_INVALID,
        ErrorCode.WORKSPACE_SYMLINK_ESCAPE,
    }:
        return api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message=str(exc),
        )
    if exc.error_code == ErrorCode.PERMISSION_DENIED:
        return api_http_exception(
            status_code=status.HTTP_403_FORBIDDEN,
            code="forbidden",
            message=str(exc),
        )
    if exc.error_code == ErrorCode.APPROVAL_EXPIRED:
        return api_http_exception(
            status_code=status.HTTP_410_GONE,
            code=exc.error_code.value,
            message=str(exc),
        )
    if exc.error_code in {ErrorCode.APPROVAL_ALREADY_RESOLVED, ErrorCode.EVENT_REPLAY_GAP}:
        return api_http_exception(
            status_code=status.HTTP_409_CONFLICT,
            code=exc.error_code.value,
            message=str(exc),
        )
    if exc.error_code == ErrorCode.DEPLOYMENT_UNAVAILABLE:
        return api_http_exception(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=exc.error_code.value,
            message=str(exc),
        )
    if exc.error_code == ErrorCode.DEPLOYMENT_FAILED:
        return api_http_exception(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code=exc.error_code.value,
            message=str(exc),
        )
    if exc.error_code in {ErrorCode.INVALID_REQUEST, ErrorCode.WORKSPACE_NOT_GIT_REPO}:
        return api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code=exc.error_code.value,
            message=str(exc),
        )
    if exc.error_code in {ErrorCode.NOT_FOUND, ErrorCode.APPROVAL_NOT_FOUND}:
        return api_http_exception(
            status_code=status.HTTP_404_NOT_FOUND,
            code=exc.error_code.value,
            message=str(exc),
        )
    if exc.error_code == ErrorCode.INVALID_STATE_TRANSITION:
        return api_http_exception(
            status_code=status.HTTP_409_CONFLICT,
            code=exc.error_code.value,
            message=str(exc),
        )
    return api_http_exception(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code=ErrorCode.UNKNOWN_ERROR.value,
        message="Internal server error.",
    )


def _default_error_code_for_status(status_code: int) -> str:
    if status_code == status.HTTP_401_UNAUTHORIZED:
        return "unauthorized"
    if status_code == status.HTTP_403_FORBIDDEN:
        return "forbidden"
    if status_code == status.HTTP_404_NOT_FOUND:
        return ErrorCode.NOT_FOUND.value
    if status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
        return ErrorCode.INVALID_REQUEST.value
    if status_code >= 500:
        return ErrorCode.UNKNOWN_ERROR.value
    return ErrorCode.INVALID_REQUEST.value


__all__ = [
    "APIErrorBody",
    "APIErrorResponse",
    "api_error_content",
    "api_error_response",
    "api_http_exception",
    "contract_http_exception",
    "error_response_doc",
    "register_error_handlers",
]
