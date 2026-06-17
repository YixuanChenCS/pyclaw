from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from packages.shared_types import DeploymentRequest, RunId, RunRequest, SessionId, WorkspaceId


class RunCreateRequestBody(BaseModel):
    workspace_id: str | None = Field(default=None, min_length=1)
    workspace_path: str | None = Field(default=None, min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    prompt: str = Field(min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    target_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_workspace_fields(self) -> "RunCreateRequestBody":
        if self.workspace_path is not None:
            return self
        if self.workspace_id is None or self.session_id is None:
            raise ValueError("workspace_path or both workspace_id and session_id must be provided")
        return self

    def to_run_request(self) -> RunRequest:
        if self.workspace_id is None or self.session_id is None:
            raise ValueError("workspace_id and session_id are required when workspace_path is not provided")
        return RunRequest(
            workspace_id=WorkspaceId(self.workspace_id),
            session_id=SessionId(self.session_id),
            prompt=self.prompt,
            run_id=RunId(self.run_id) if self.run_id is not None else None,
            target_paths=tuple(self.target_paths),
        )


class RunAcceptedResponse(BaseModel):
    run_id: str
    status: str


class HealthResponse(BaseModel):
    service: str
    status: str
    checked_at: str
    details: dict[str, Any] = Field(default_factory=dict)


class RecoveryStatusResponse(BaseModel):
    run_id: str
    recovery_state: str
    reason: str
    task_id: str | None = None
    recovery_options: list[str] = Field(default_factory=list)
    rollback_task_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ApprovalDecisionRequestBody(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str | None = None


class DeploymentCreateRequestBody(BaseModel):
    run_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    target: str = Field(min_length=1)

    def to_deployment_request(self) -> DeploymentRequest:
        return DeploymentRequest(
            run_id=RunId(self.run_id),
            workspace_id=WorkspaceId(self.workspace_id),
            target=self.target,
        )


class ArtifactSummaryResponse(BaseModel):
    artifact_id: str
    run_id: str
    artifact_type: str
    label: str | None = None
    uri: str | None = None
    size_bytes: int | None = None
    created_at: str | None = None


class ArtifactDetailResponse(ArtifactSummaryResponse):
    content: Any | None = None
    content_inline: bool = False
    content_kind: Literal["text", "json", "binary"] | None = None
    content_note: str | None = None
    download_uri: str | None = None


class RunSummaryResponse(BaseModel):
    run_id: str
    status: str
    summary: str | None = None
    completed: bool


class RunResultResponse(BaseModel):
    run_id: str
    status: str
    summary: str | None = None
    artifacts: list[ArtifactSummaryResponse] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None


class RunEventResponse(BaseModel):
    run_id: str
    event_type: str
    event_id: str
    sequence: int
    message: str | None = None
    run_status: str | None = None
    task_id: str | None = None
    task_status: str | None = None
    artifact_id: str | None = None
    approval_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


class ApprovalRecordResponse(BaseModel):
    approval_id: str
    run_id: str
    status: str
    kind: str
    reason: str
    task_id: str | None = None
    patch_id: str | None = None
    command_argv: list[str] = Field(default_factory=list)
    approved: bool | None = None
    created_at: str | None = None
    updated_at: str | None = None
    decided_at: str | None = None
    expires_at: str | None = None
    reviewer: str | None = None
    comment: str | None = None


class DeploymentResultResponse(BaseModel):
    run_id: str
    status: str
    url: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


_DEFAULT_LOCAL_CORS_ORIGINS = (
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


class APIConfig(BaseModel):
    allowed_workspace_roots: tuple[str, ...] = ()
    api_token: str | None = None
    auth_principals: tuple["APIAuthPrincipal", ...] = ()
    cors_allowed_origins: tuple[str, ...] = _DEFAULT_LOCAL_CORS_ORIGINS

    def auth_enabled(self) -> bool:
        return self.api_token is not None or bool(self.auth_principals)


class APIAuthPrincipal(BaseModel):
    token: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    permissions: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def validate_permissions(self) -> "APIAuthPrincipal":
        if not self.permissions:
            raise ValueError("auth principal permissions must not be empty")
        normalized = tuple(permission.strip() for permission in self.permissions if permission.strip())
        if not normalized:
            raise ValueError("auth principal permissions must not be empty")
        self.permissions = normalized
        return self


__all__ = [
    "APIConfig",
    "APIAuthPrincipal",
    "ApprovalDecisionRequestBody",
    "ApprovalRecordResponse",
    "ArtifactDetailResponse",
    "ArtifactSummaryResponse",
    "DeploymentCreateRequestBody",
    "DeploymentResultResponse",
    "HealthResponse",
    "RecoveryStatusResponse",
    "RunAcceptedResponse",
    "RunCreateRequestBody",
    "RunEventResponse",
    "RunResultResponse",
    "RunSummaryResponse",
]
