from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from fastapi import HTTPException, Request, status

from packages.shared_types import ErrorCode, ErrorCodeContractError, HealthCheckResult, RunEvent

from apps.api.errors import api_http_exception, contract_http_exception, error_response_doc
from apps.api.platform_api import PlatformAPI
from apps.api.schemas import APIConfig


def get_platform_api(request: Request) -> PlatformAPI:
    platform_api = getattr(request.app.state, "platform_api", None)
    if platform_api is None:
        raise RuntimeError("Platform API is not configured")
    return cast(PlatformAPI, platform_api)


def get_api_config(request: Request) -> APIConfig:
    return cast(APIConfig, request.app.state.api_config)


def route_responses(*response_items: tuple[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {status_code: payload for status_code, payload in response_items}


def unauthorized_response() -> tuple[int, dict[str, Any]]:
    return (
        status.HTTP_401_UNAUTHORIZED,
        error_response_doc(
            description="Missing or invalid bearer token.",
            code="unauthorized",
            message="Missing or invalid bearer token.",
        ),
    )


def forbidden_response(permission: str) -> tuple[int, dict[str, Any]]:
    return (
        status.HTTP_403_FORBIDDEN,
        error_response_doc(
            description="The authenticated principal lacks the required permission.",
            code="forbidden",
            message=f"Permission denied. Missing required permission: {permission}",
        ),
    )


def validation_error_response() -> tuple[int, dict[str, Any]]:
    return (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        error_response_doc(
            description="The request failed validation.",
            code=ErrorCode.INVALID_REQUEST.value,
            message="Request validation failed.",
        ),
    )


def status_code_for_health(health: HealthCheckResult) -> int:
    return status.HTTP_200_OK if health.status == "ready" else status.HTTP_503_SERVICE_UNAVAILABLE


def validate_workspace_path(workspace_path: str, *, config: APIConfig) -> str:
    raw_path = Path(workspace_path).expanduser()
    if not workspace_path.strip():
        raise api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message="Workspace path must not be empty.",
        )
    try:
        resolved_path = raw_path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message=f"Workspace path is invalid: {workspace_path}",
        ) from exc
    if not raw_path.exists():
        raise api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message=f"Workspace path does not exist: {resolved_path}",
        )
    if not raw_path.is_dir():
        raise api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message=f"Workspace path must be a directory: {resolved_path}",
        )
    if raw_path.is_symlink():
        raise api_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_workspace",
            message=f"Workspace path resolves through a symlink: {workspace_path}",
        )
    if config.allowed_workspace_roots:
        resolved_roots = tuple(Path(root).expanduser().resolve(strict=False) for root in config.allowed_workspace_roots)
        if not any(is_within_root(resolved_path, root) for root in resolved_roots):
            raise api_http_exception(
                status_code=status.HTTP_403_FORBIDDEN,
                code="workspace_not_allowed",
                message=f"Workspace path is outside allowed roots: {resolved_path}",
            )
    return str(resolved_path)


def is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def run_creation_http_exception(exc: ErrorCodeContractError) -> HTTPException:
    if exc.error_code == ErrorCode.INVALID_REQUEST and exc.details.get("run_id"):
        return api_http_exception(
            status_code=status.HTTP_409_CONFLICT,
            code=exc.error_code.value,
            message=str(exc),
        )
    return contract_http_exception(exc)


def format_sse_event(event: RunEvent) -> str:
    payload = json.dumps(event.to_dict(), separators=(",", ":"))
    return f"id: {event.event_id}\nevent: {event.event_type.value}\ndata: {payload}\n\n"


__all__ = [
    "format_sse_event",
    "forbidden_response",
    "get_api_config",
    "get_platform_api",
    "route_responses",
    "run_creation_http_exception",
    "status_code_for_health",
    "unauthorized_response",
    "validate_workspace_path",
    "validation_error_response",
]
