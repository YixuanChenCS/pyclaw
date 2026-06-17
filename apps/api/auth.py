from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from fastapi import Request, status

from apps.api.errors import api_http_exception
from apps.api.schemas import APIAuthPrincipal, APIConfig


_PUBLIC_PATHS = {
    "/health",
    "/metrics",
    "/metrics/runtime",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}

_READ_PERMISSIONS = frozenset(
    {
        "runs:read",
        "events:read",
        "approvals:read",
        "artifacts:read",
    }
)

_WRITE_PERMISSIONS = frozenset(
    {
        "runs:create",
        "runs:cancel",
        "runs:recover",
        "approvals:decide",
        "deployments:create",
    }
)

_ALL_PERMISSIONS = _READ_PERMISSIONS | _WRITE_PERMISSIONS


@dataclass(frozen=True, slots=True)
class AuthContext:
    subject: str
    auth_scheme: str = "bearer"
    permissions: tuple[str, ...] = ()

    def has_permission(self, permission: str) -> bool:
        return permission in set(self.permissions)


def is_public_request(request: Request) -> bool:
    return request.method == "OPTIONS" or request.url.path in _PUBLIC_PATHS


def authenticate_request(request: Request, config: APIConfig) -> AuthContext | None:
    if is_public_request(request):
        return None
    if not config.auth_enabled():
        return None
    auth_header = request.headers.get("Authorization")
    bearer_prefix = "Bearer "
    if auth_header is None or not auth_header.startswith(bearer_prefix):
        return None
    token = auth_header[len(bearer_prefix):]
    for principal in resolve_auth_principals(config):
        if token == principal.token:
            return AuthContext(
                subject=principal.subject,
                permissions=expand_permissions(principal.permissions),
            )
    return None


def resolve_auth_principals(config: APIConfig) -> tuple[APIAuthPrincipal, ...]:
    principals = list(config.auth_principals)
    if config.api_token is not None and all(principal.token != config.api_token for principal in principals):
        principals.append(
            APIAuthPrincipal(
                token=config.api_token,
                subject="api-token",
                permissions=("control_plane:admin",),
            )
        )
    return tuple(principals)


@lru_cache(maxsize=64)
def expand_permissions(permissions: tuple[str, ...]) -> tuple[str, ...]:
    expanded = set(permissions)
    if "control_plane:admin" in expanded:
        expanded.update(_ALL_PERMISSIONS)
        expanded.add("control_plane:read")
        expanded.add("control_plane:write")
    if "control_plane:write" in expanded:
        expanded.update(_WRITE_PERMISSIONS)
        expanded.update(_READ_PERMISSIONS)
        expanded.add("control_plane:read")
    if "control_plane:read" in expanded:
        expanded.update(_READ_PERMISSIONS)
    return tuple(sorted(expanded))


def require_permission(permission: str):
    async def dependency(request: Request) -> AuthContext | None:
        config = request.app.state.api_config
        if not isinstance(config, APIConfig):
            raise RuntimeError("API config is not configured")
        if not config.auth_enabled():
            return None
        auth_context = getattr(request.state, "auth_context", None)
        if not isinstance(auth_context, AuthContext):
            raise api_http_exception(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="unauthorized",
                message="Missing or invalid bearer token.",
            )
        if not auth_context.has_permission(permission):
            raise api_http_exception(
                status_code=status.HTTP_403_FORBIDDEN,
                code="forbidden",
                message=f"Permission denied. Missing required permission: {permission}",
            )
        return auth_context

    return dependency


__all__ = [
    "AuthContext",
    "authenticate_request",
    "expand_permissions",
    "is_public_request",
    "require_permission",
    "resolve_auth_principals",
]
