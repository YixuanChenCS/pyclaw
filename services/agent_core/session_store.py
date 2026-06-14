from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
import json
from typing import Any, Mapping

from packages.shared_types import (
    ArtifactRef,
    ArtifactType,
    FileSummary,
    RepoContextResult,
    SymbolMatch,
    TaskStatus,
)
from packages.shared_types.ids import ArtifactId, RunId, TaskId, WorkspaceId

from .models import (
    AgentAction,
    AgentActionType,
    AgentContextBudget,
    AgentFailure,
    AgentPlan,
    AgentSession,
    AgentSessionPhase,
    AgentStep,
)
from .validation import validate_session_basic_shape

AGENT_SESSION_SCHEMA_VERSION = 1


class AgentSessionStore(ABC):
    @abstractmethod
    async def save_agent_session(self, session: AgentSession) -> None:
        """Persist the canonical serialized session snapshot for a run."""

    @abstractmethod
    async def load_agent_session(self, run_id: RunId | str) -> AgentSession | None:
        """Load the canonical session snapshot for a run, if one exists."""


def serialize_agent_session(session: AgentSession) -> str:
    validate_session_basic_shape(session)
    return json.dumps(session.to_dict(), sort_keys=True)


def deserialize_agent_session_json(
    payload_json: str,
    *,
    schema_version: int = AGENT_SESSION_SCHEMA_VERSION,
) -> AgentSession:
    if schema_version != AGENT_SESSION_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported AgentSession schema version {schema_version}; "
            f"expected {AGENT_SESSION_SCHEMA_VERSION}"
        )

    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise ValueError("Serialized AgentSession payload must be a JSON object")
    return deserialize_agent_session_dict(payload)


def deserialize_agent_session_dict(payload: Mapping[str, Any]) -> AgentSession:
    repo_context_payload = payload.get("repo_context")
    current_plan_payload = payload.get("current_plan")
    context_budget_payload = payload.get("context_budget")

    session = AgentSession(
        run_id=RunId(str(payload["run_id"])),
        workspace_id=WorkspaceId(str(payload["workspace_id"])),
        user_request=str(payload["user_request"]),
        phase=_enum_value(
            AgentSessionPhase,
            payload.get(
                "phase",
                AgentSessionPhase.READY.value if isinstance(current_plan_payload, Mapping) else AgentSessionPhase.PLANNING.value,
            ),
        ),
        repo_context=_repo_context_from_dict(repo_context_payload)
        if isinstance(repo_context_payload, Mapping)
        else None,
        current_plan=_agent_plan_from_dict(current_plan_payload)
        if isinstance(current_plan_payload, Mapping)
        else None,
        prior_artifacts=[
            _artifact_ref_from_dict(item)
            for item in _require_mapping_list(payload.get("prior_artifacts", []), "prior_artifacts")
        ],
        action_history=[
            _agent_action_from_dict(item)
            for item in _require_mapping_list(payload.get("action_history", []), "action_history")
        ],
        pending_action=_agent_action_from_dict(payload["pending_action"])
        if isinstance(payload.get("pending_action"), Mapping)
        else None,
        pending_approval_id=_optional_str(payload.get("pending_approval_id")),
        completed_action_ids=_string_list(payload.get("completed_action_ids", []), "completed_action_ids"),
        iteration_count=int(payload.get("iteration_count", 0)),
        failure_history=[
            _agent_failure_from_dict(item)
            for item in _require_mapping_list(payload.get("failure_history", []), "failure_history")
        ],
        warnings=_string_list(payload.get("warnings", []), "warnings"),
        context_budget=_context_budget_from_dict(context_budget_payload)
        if isinstance(context_budget_payload, Mapping)
        else None,
        created_at=_parse_datetime(payload.get("created_at")),
    )
    validate_session_basic_shape(session)
    return session


def _agent_plan_from_dict(payload: Mapping[str, Any]) -> AgentPlan:
    return AgentPlan(
        goal=str(payload["goal"]),
        steps=[
            _agent_step_from_dict(item)
            for item in _require_mapping_list(payload.get("steps", []), "steps")
        ],
        summary=_optional_str(payload.get("summary")),
    )


def _agent_step_from_dict(payload: Mapping[str, Any]) -> AgentStep:
    return AgentStep(
        kind=str(payload["kind"]),
        description=str(payload["description"]),
        step_id=_optional_str(payload.get("step_id")),
        target_files=tuple(_string_list(payload.get("target_files", []), "target_files")),
        rationale=_optional_str(payload.get("rationale")),
        status=_enum_value(TaskStatus, payload.get("status", TaskStatus.PENDING.value)),
    )


def _agent_action_from_dict(payload: Mapping[str, Any]) -> AgentAction:
    return AgentAction(
        type=_enum_value(AgentActionType, payload["type"]),
        reason=str(payload["reason"]),
        step_id=_optional_str(payload.get("step_id")),
        action_id=_optional_str(payload.get("action_id")),
        target_files=tuple(_string_list(payload.get("target_files", []), "target_files")),
        command_argv=tuple(_string_list(payload.get("command_argv", []), "command_argv")),
        cwd=_optional_str(payload.get("cwd")),
        patch_diff=_optional_str(payload.get("patch_diff")),
        allow_file_deletions=bool(payload.get("allow_file_deletions", False)),
        approval_message=_optional_str(payload.get("approval_message")),
        approval_risk_reason=_optional_str(payload.get("approval_risk_reason")),
        summary_text=_optional_str(payload.get("summary_text")),
        requested_context=tuple(_string_list(payload.get("requested_context", []), "requested_context")),
    )


def _agent_failure_from_dict(payload: Mapping[str, Any]) -> AgentFailure:
    details = payload.get("details", {})
    if details is None:
        details = {}
    if not isinstance(details, Mapping):
        raise ValueError("AgentFailure.details must be an object")
    return AgentFailure(
        stage=str(payload["stage"]),
        message=str(payload["message"]),
        code=_optional_str(payload.get("code")),
        retryable=bool(payload.get("retryable", False)),
        details={str(key): str(value) for key, value in details.items()},
    )


def _context_budget_from_dict(payload: Mapping[str, Any]) -> AgentContextBudget:
    return AgentContextBudget(
        max_input_tokens=_optional_int(payload.get("max_input_tokens")),
        max_output_tokens=_optional_int(payload.get("max_output_tokens")),
        remaining_input_tokens=_optional_int(payload.get("remaining_input_tokens")),
        remaining_output_tokens=_optional_int(payload.get("remaining_output_tokens")),
    )


def _repo_context_from_dict(payload: Mapping[str, Any]) -> RepoContextResult:
    return RepoContextResult(
        workspace_id=WorkspaceId(str(payload["workspace_id"])),
        run_id=RunId(str(payload["run_id"])),
        file_summaries=tuple(
            FileSummary(
                path=str(item["path"]),
                summary=_optional_str(item.get("summary")),
                language=_optional_str(item.get("language")),
                content=_optional_str(item.get("content")),
            )
            for item in _require_mapping_list(payload.get("file_summaries", []), "file_summaries")
        ),
        repo_map=_optional_str(payload.get("repo_map")),
        symbols=tuple(
            SymbolMatch(
                name=str(item["name"]),
                kind=str(item["kind"]),
                path=str(item["path"]),
                line=_optional_int(item.get("line")),
            )
            for item in _require_mapping_list(payload.get("symbols", []), "symbols")
        ),
        dependency_hints=tuple(_string_list(payload.get("dependency_hints", []), "dependency_hints")),
        warnings=tuple(_string_list(payload.get("warnings", []), "warnings")),
        created_at=_parse_datetime(payload.get("created_at")),
    )


def _artifact_ref_from_dict(payload: Mapping[str, Any]) -> ArtifactRef:
    task_id = payload.get("task_id")
    return ArtifactRef(
        artifact_id=ArtifactId(str(payload["artifact_id"])),
        run_id=RunId(str(payload["run_id"])),
        artifact_type=_enum_value(ArtifactType, payload["artifact_type"]),
        task_id=TaskId(str(task_id)) if task_id is not None else None,
        label=_optional_str(payload.get("label")),
        uri=_optional_str(payload.get("uri")),
        created_at=_parse_datetime(payload.get("created_at")),
    )


def _require_mapping_list(value: Any, field_name: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    result: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{field_name} items must be objects")
        result.append(item)
    return result


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must contain only strings")
        result.append(item)
    return result


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Expected string or null")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError("Expected integer or null")
    return value


def _enum_value(enum_type: type[Enum], value: Any):
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Expected string enum value for {enum_type.__name__}")
    return enum_type(value)


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Expected ISO datetime string")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
