from __future__ import annotations

from enum import Enum


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    NEEDS_RECOVERY = "needs_recovery"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class EventType(str, Enum):
    RUN_CREATED = "run.created"
    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    RUN_STATUS_CHANGED = "run.status_changed"
    RUN_NEEDS_RECOVERY = "run.needs_recovery"
    REPO_CONTEXT_BUILT = "repo.context_built"
    AGENT_PLAN = "agent.plan"
    AGENT_MESSAGE = "agent.message"
    AGENT_ACTION_REQUESTED = "agent.action_requested"
    COMMAND_STARTED = "command.started"
    COMMAND_COMPLETED = "command.completed"
    COMMAND_FAILED = "command.failed"
    COMMAND_TIMEOUT = "command.timeout"
    COMMAND_CANCELLED = "command.cancelled"
    PATCH_PROPOSED = "patch.proposed"
    PATCH_APPLIED = "patch.applied"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    ARTIFACT_CREATED = "artifact.created"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


class ArtifactType(str, Enum):
    SUMMARY = "summary"
    PATCH = "patch"
    DIFF = "diff"
    LOG = "log"
    COMMAND_OUTPUT = "command_output"
    TEST_RESULT = "test_result"


class RecoveryState(str, Enum):
    NEEDS_RECOVERY = "needs_recovery"
    ROLLBACK_AVAILABLE = "rollback_available"
    ROLLBACK_REQUIRED_REVIEW = "rollback_required_review"


class RecoveryOption(str, Enum):
    RESUME_IF_SAFE = "resume_if_safe"
    REVIEW_MANUALLY = "review_manually"
    ROLLBACK_IF_AVAILABLE = "rollback_if_available"
    ABORT = "abort"


__all__ = [
    "ArtifactType",
    "EventType",
    "RecoveryOption",
    "RecoveryState",
    "RunStatus",
    "TaskStatus",
]
