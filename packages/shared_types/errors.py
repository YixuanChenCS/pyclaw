from __future__ import annotations

from enum import Enum
from typing import Mapping


class ErrorCode(str, Enum):
    UNKNOWN_ERROR = "unknown_error"
    INVALID_REQUEST = "invalid_request"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    WORKSPACE_NOT_GIT_REPO = "workspace_not_git_repo"
    WORKSPACE_PATH_INVALID = "workspace_path_invalid"
    WORKSPACE_SYMLINK_ESCAPE = "workspace_symlink_escape"
    WORKSPACE_LOCK_CONFLICT = "workspace_lock_conflict"
    WORKSPACE_BRANCH_CHANGED = "workspace_branch_changed"
    WORKSPACE_COMMIT_CHANGED = "workspace_commit_changed"
    WORKSPACE_FILE_CHANGED = "workspace_file_changed"
    WORKSPACE_FILE_DELETED = "workspace_file_deleted"
    WORKSPACE_FILE_TOO_LARGE = "workspace_file_too_large"
    WORKSPACE_BINARY_FILE = "workspace_binary_file"
    WORKSPACE_GENERATED_OR_VENDOR_FILE = "workspace_generated_or_vendor_file"
    REPO_CONTEXT_OVERFLOW = "repo_context_overflow"
    REPO_INDEX_STALE = "repo_index_stale"
    REPO_INDEX_FAILED = "repo_index_failed"
    SYMBOL_SEARCH_FAILED = "symbol_search_failed"
    MODEL_RESPONSE_MALFORMED = "model_response_malformed"
    MODEL_CONTEXT_OVERFLOW = "model_context_overflow"
    MODEL_PROVIDER_ERROR = "model_provider_error"
    MODEL_RATE_LIMITED = "model_rate_limited"
    MODEL_REPEATED_FAILURE_LOOP = "model_repeated_failure_loop"
    AGENT_INVALID_ACTION = "agent_invalid_action"
    AGENT_UNKNOWN_FILE_REFERENCE = "agent_unknown_file_reference"
    AGENT_PATCH_REFERENCES_MISSING_FILE = "agent_patch_references_missing_file"
    AGENT_WRITE_OUTSIDE_WORKSPACE = "agent_write_outside_workspace"
    COMMAND_TIMEOUT = "command_timeout"
    COMMAND_FAILED = "command_failed"
    COMMAND_OUTPUT_TOO_LARGE = "command_output_too_large"
    COMMAND_CANCELLED = "command_cancelled"
    SHELL_DISABLED = "shell_disabled"
    DUPLICATE_JOB_DELIVERY = "duplicate_job_delivery"
    WORKER_CRASHED = "worker_crashed"
    WORKER_LOST_LOCK = "worker_lost_lock"
    PATCH_MALFORMED = "patch_malformed"
    PATCH_APPLY_FAILED = "patch_apply_failed"
    PATCH_CONFLICT = "patch_conflict"
    PATCH_PERMISSION_DENIED = "patch_permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_NOT_FOUND = "approval_not_found"
    APPROVAL_ALREADY_RESOLVED = "approval_already_resolved"
    APPROVAL_EXPIRED = "approval_expired"
    PERSISTENCE_ERROR = "persistence_error"
    EVENT_ORDER_VIOLATION = "event_order_violation"
    EVENT_REPLAY_GAP = "event_replay_gap"
    ARTIFACT_STORE_ERROR = "artifact_store_error"


class ContractError(Exception):
    """Base exception for shared contract failures."""


class ErrorCodeContractError(ContractError):
    """Raised when a contract boundary can classify a failure with a stable error code."""

    def __init__(
        self,
        error_code: ErrorCode,
        message: str,
        *,
        details: Mapping[str, str] | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.details = dict(details or {})


class ContractSerializationError(ContractError):
    """Raised when a contract model cannot be serialized safely."""


class EntityNotFoundError(ContractError):
    """Raised when a referenced entity cannot be loaded."""

    def __init__(self, entity_name: str, entity_id: str):
        super().__init__(f"{entity_name} not found: {entity_id}")
        self.entity_name = entity_name
        self.entity_id = entity_id


class InvalidRunStateError(ContractError):
    """Raised when code attempts an invalid run lifecycle transition."""


class WorkspaceLockedError(ContractError):
    """Raised when a workspace lock cannot be acquired."""


__all__ = [
    "ContractError",
    "ContractSerializationError",
    "ErrorCode",
    "ErrorCodeContractError",
    "EntityNotFoundError",
    "InvalidRunStateError",
    "WorkspaceLockedError",
]
