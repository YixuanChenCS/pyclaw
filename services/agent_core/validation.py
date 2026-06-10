from __future__ import annotations

import json
from typing import Mapping, Sequence

from .models import AgentActionType, AgentSession

VALID_PLAN_STEP_KINDS = frozenset(
    {"inspect", "patch", "command", "approval", "summarize", "complete"}
)
MAX_AGENT_PLAN_STEPS = 8


class AgentCoreValidationError(ValueError):
    """Base validation error for agent-core structured outputs."""


class AgentPlanValidationError(AgentCoreValidationError):
    """Raised when a model-produced plan is malformed or unsupported."""


def validate_action_type(action_type: AgentActionType | str) -> AgentActionType:
    if isinstance(action_type, AgentActionType):
        return action_type

    try:
        return AgentActionType(action_type)
    except ValueError as exc:
        raise ValueError(f"Unsupported agent action type: {action_type!r}") from exc


def validate_session_basic_shape(session: AgentSession) -> None:
    if not str(session.run_id):
        raise ValueError("AgentSession.run_id must be set")
    if not str(session.workspace_id):
        raise ValueError("AgentSession.workspace_id must be set")
    if not session.user_request.strip():
        raise ValueError("AgentSession.user_request must be non-empty")
    if session.iteration_count < 0:
        raise ValueError("AgentSession.iteration_count must be non-negative")


def parse_json_object(text: str) -> Mapping[str, object]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentPlanValidationError("Model returned malformed JSON") from exc

    if not isinstance(payload, dict):
        raise AgentPlanValidationError("Plan response must be a JSON object")
    return payload


def validate_plan_payload(payload: Mapping[str, object]) -> dict[str, object]:
    goal_value = payload.get("goal")
    if not isinstance(goal_value, str) or not goal_value.strip():
        raise AgentPlanValidationError("Plan goal must be a non-empty string")

    steps_value = payload.get("steps")
    if not isinstance(steps_value, list) or not steps_value:
        raise AgentPlanValidationError("Plan steps must be a non-empty list")
    if len(steps_value) > MAX_AGENT_PLAN_STEPS:
        raise AgentPlanValidationError(
            f"Plan steps may not exceed {MAX_AGENT_PLAN_STEPS} items"
        )

    normalized_steps: list[dict[str, object]] = []
    for index, raw_step in enumerate(steps_value, start=1):
        if not isinstance(raw_step, dict):
            raise AgentPlanValidationError(f"Plan step {index} must be an object")

        raw_kind = raw_step.get("kind", raw_step.get("type"))
        if not isinstance(raw_kind, str) or not raw_kind.strip():
            raise AgentPlanValidationError(f"Plan step {index} must include a non-empty kind")

        kind = raw_kind.strip().lower()
        if kind not in VALID_PLAN_STEP_KINDS:
            raise AgentPlanValidationError(f"Plan step {index} has unsupported kind {kind!r}")

        description = raw_step.get("description")
        if not isinstance(description, str) or not description.strip():
            raise AgentPlanValidationError(
                f"Plan step {index} must include a non-empty description"
            )

        target_files_value = raw_step.get("target_files", ())
        if target_files_value is None:
            target_files: tuple[str, ...] = ()
        elif (
            isinstance(target_files_value, Sequence)
            and not isinstance(target_files_value, (str, bytes, bytearray))
        ):
            cleaned_paths = []
            for path_index, raw_path in enumerate(target_files_value, start=1):
                if not isinstance(raw_path, str) or not raw_path.strip():
                    raise AgentPlanValidationError(
                        f"Plan step {index} target_files[{path_index}] must be a non-empty string"
                    )
                cleaned_paths.append(raw_path.strip())
            target_files = tuple(cleaned_paths)
        else:
            raise AgentPlanValidationError(
                f"Plan step {index} target_files must be a sequence of strings"
            )

        rationale_value = raw_step.get("rationale")
        rationale: str | None
        if rationale_value is None:
            rationale = None
        elif not isinstance(rationale_value, str) or not rationale_value.strip():
            raise AgentPlanValidationError(
                f"Plan step {index} rationale must be a non-empty string when provided"
            )
        else:
            rationale = rationale_value.strip()

        normalized_steps.append(
            {
                "kind": kind,
                "description": description.strip(),
                "target_files": target_files,
                "rationale": rationale,
            }
        )

    summary_value = payload.get("summary")
    summary: str | None
    if summary_value is None:
        summary = None
    elif not isinstance(summary_value, str) or not summary_value.strip():
        raise AgentPlanValidationError("Plan summary must be a non-empty string when provided")
    else:
        summary = summary_value.strip()

    return {
        "goal": goal_value.strip(),
        "steps": normalized_steps,
        "summary": summary,
    }
