from __future__ import annotations

from enum import Enum


class RunMode(str, Enum):
    ASK = "ask"
    EDIT = "edit"
    PLAN = "plan"
    DEPLOY = "deploy"
    MONITOR = "monitor"


class RunStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class ActionKind(str, Enum):
    PLAN = "plan"
    REQUEST_CONTEXT = "request_context"
    EXECUTE_COMMAND = "execute_command"
    PROPOSE_PATCH = "propose_patch"
    REQUEST_APPROVAL = "request_approval"
    SUMMARIZE = "summarize"
    COMPLETE = "complete"


class EventType(str, Enum):
    RUN_CREATED = "run.created"
    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    CONTEXT_GENERATED = "context.generated"
    AGENT_PLAN = "agent.plan"
    AGENT_ACTION = "agent.action"
    TOOL_COMMAND_STARTED = "tool.command.started"
    TOOL_COMMAND_FINISHED = "tool.command.finished"
    PATCH_PROPOSED = "patch.proposed"
    PATCH_APPLIED = "patch.applied"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_RESOLVED = "approval.resolved"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    DEPLOYMENT_STARTED = "deployment.started"
    DEPLOYMENT_FINISHED = "deployment.finished"
    HEALTH_UPDATED = "health.updated"


class ApprovalMode(str, Enum):
    NEVER = "never"
    ON_WRITE = "on_write"
    ON_COMMAND = "on_command"
    ON_DEPLOY = "on_deploy"
    ALWAYS = "always"


class ArtifactType(str, Enum):
    LOG = "log"
    PATCH = "patch"
    COMMAND_RESULT = "command_result"
    TEST_RESULT = "test_result"
    REPO_CONTEXT = "repo_context"
    SUMMARY = "summary"
    DEPLOYMENT = "deployment"
    HEALTH_REPORT = "health_report"


class FailureCode(str, Enum):
    UNKNOWN = "unknown"
    REPO_NOT_FOUND = "repo_not_found"
    MULTI_REPO_INPUT = "multi_repo_input"
    INDEX_TOO_LARGE = "index_too_large"
    BINARY_FILE_INCLUDED = "binary_file_included"
    FILE_CHANGED_DURING_RUN = "file_changed_during_run"
    PATCH_CONFLICT = "patch_conflict"
    COMMAND_TIMEOUT = "command_timeout"
    COMMAND_OUTPUT_LIMIT = "command_output_limit"
    WORKER_CRASHED = "worker_crashed"
    DUPLICATE_JOB_DELIVERY = "duplicate_job_delivery"
    STREAM_DISCONNECTED = "stream_disconnected"
    MALFORMED_MODEL_RESPONSE = "malformed_model_response"
    MISSING_FILE = "missing_file"
    CONTEXT_OVERFLOW = "context_overflow"
    APPROVAL_EXPIRED = "approval_expired"
    RUN_CANCELLED = "run_cancelled"
    SECRET_DETECTED = "secret_detected"
    CONCURRENT_WORKSPACE_MODIFICATION = "concurrent_workspace_modification"
    DEPLOYMENT_FAILED = "deployment_failed"
    HEALTH_CHECK_FAILED = "health_check_failed"
    STALE_INDEX = "stale_index"


class DeploymentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class HealthState(str, Enum):
    UNKNOWN = "unknown"
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
