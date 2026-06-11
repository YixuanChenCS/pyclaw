from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Mapping

from packages.shared_types import (
    ErrorCode,
    ErrorCodeContractError,
    EventType,
    JSONValue,
    RunEvent,
    RunStatus,
)


def serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def run_status_event_type(status: RunStatus) -> EventType:
    return {
        RunStatus.QUEUED: EventType.RUN_QUEUED,
        RunStatus.RUNNING: EventType.RUN_STARTED,
        RunStatus.NEEDS_RECOVERY: EventType.RUN_NEEDS_RECOVERY,
        RunStatus.CANCELLED: EventType.RUN_CANCELLED,
        RunStatus.SUCCEEDED: EventType.RUN_COMPLETED,
        RunStatus.FAILED: EventType.RUN_FAILED,
    }.get(status, EventType.RUN_STATUS_CHANGED)


def json_dumps(value: Mapping[str, JSONValue]) -> str:
    return json.dumps(value, sort_keys=True)


def json_loads(value: str) -> dict[str, JSONValue]:
    loaded = json.loads(value)
    if isinstance(loaded, dict):
        return loaded
    raise ValueError("Expected JSON object payload")


def validate_next_event_sequence(run_id: str, previous_sequence: int, event: RunEvent) -> int:
    expected_sequence = previous_sequence + 1
    if event.sequence == expected_sequence:
        return event.sequence

    error_code = (
        ErrorCode.EVENT_REPLAY_GAP
        if event.sequence > expected_sequence
        else ErrorCode.EVENT_ORDER_VIOLATION
    )
    raise ErrorCodeContractError(
        error_code,
        f"Run event sequence violation for {run_id}: expected {expected_sequence}, got {event.sequence}",
        details={
            "run_id": str(run_id),
            "expected_sequence": str(expected_sequence),
            "actual_sequence": str(event.sequence),
        },
    )


__all__ = [
    "json_dumps",
    "json_loads",
    "parse_datetime",
    "run_status_event_type",
    "serialize_datetime",
    "validate_next_event_sequence",
]
