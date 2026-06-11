from __future__ import annotations

from packages.shared_types import InvalidRunStateError, RunStatus

TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.CANCELLED,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
    }
)

ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
        }
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.QUEUED,
            RunStatus.WAITING_FOR_APPROVAL,
            RunStatus.NEEDS_RECOVERY,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
        }
    ),
    RunStatus.WAITING_FOR_APPROVAL: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.NEEDS_RECOVERY,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
        }
    ),
    RunStatus.NEEDS_RECOVERY: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.WAITING_FOR_APPROVAL,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
        }
    ),
    RunStatus.CANCELLING: frozenset({RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
}


def validate_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in ALLOWED_RUN_TRANSITIONS[current]:
        raise InvalidRunStateError(f"Invalid run transition: {current.value} -> {target.value}")


__all__ = [
    "ALLOWED_RUN_TRANSITIONS",
    "TERMINAL_RUN_STATUSES",
    "validate_run_transition",
]
