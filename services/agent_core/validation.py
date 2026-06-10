from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Mapping, Sequence

from packages.shared_types import TaskStatus

from .models import AgentAction, AgentActionType, AgentSession, LoopGuardResult

VALID_PLAN_STEP_KINDS = frozenset(
    {"inspect", "patch", "command", "approval", "summarize", "complete"}
)
MAX_AGENT_PLAN_STEPS = 8
MAX_AGENT_ITERATIONS = 12
MAX_REPEATED_ACTIONS = 3
MAX_REPEATED_COMMAND_FAILURES = 2
MAX_REPEATED_PATCH_REVIEW_FAILURES = 2
MAX_NO_PROGRESS_ITERATIONS = 3


class AgentCoreValidationError(ValueError):
    """Base validation error for agent-core structured outputs."""


class AgentPlanValidationError(AgentCoreValidationError):
    """Raised when a model-produced plan is malformed or unsupported."""


class AgentStateValidationError(AgentCoreValidationError):
    """Raised when session state is inconsistent for deterministic action selection."""


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


def validate_next_action_session(session: AgentSession) -> None:
    validate_session_basic_shape(session)

    plan = session.current_plan
    if plan is None:
        raise AgentStateValidationError("AgentSession.current_plan must be set for next_action")
    if not plan.goal.strip():
        raise AgentStateValidationError("AgentSession.current_plan.goal must be non-empty")
    if not plan.steps:
        raise AgentStateValidationError("AgentSession.current_plan.steps must be non-empty")

    running_steps = 0
    first_pending_seen = False
    for index, step in enumerate(plan.steps, start=1):
        if not isinstance(step.kind, str) or not step.kind.strip():
            raise AgentStateValidationError(f"Plan step {index} must include a non-empty kind")

        kind = step.kind.strip().lower()
        if kind not in VALID_PLAN_STEP_KINDS:
            raise AgentStateValidationError(f"Plan step {index} has unsupported kind {kind!r}")

        if not isinstance(step.description, str) or not step.description.strip():
            raise AgentStateValidationError(
                f"Plan step {index} must include a non-empty description"
            )

        if not isinstance(step.status, TaskStatus):
            raise AgentStateValidationError(f"Plan step {index} has invalid status {step.status!r}")

        for path_index, path in enumerate(step.target_files, start=1):
            if not isinstance(path, str) or not path.strip():
                raise AgentStateValidationError(
                    f"Plan step {index} target_files[{path_index}] must be a non-empty string"
                )

        if kind == "patch" and not step.target_files:
            raise AgentStateValidationError(
                f"Plan step {index} kind 'patch' requires at least one target file"
            )

        if step.status == TaskStatus.RUNNING:
            running_steps += 1
        elif step.status == TaskStatus.PENDING:
            first_pending_seen = True
        elif first_pending_seen and step.status == TaskStatus.SUCCEEDED:
            raise AgentStateValidationError(
                f"Plan step {index} cannot be succeeded after a pending step"
            )

    if running_steps:
        raise AgentStateValidationError("next_action cannot run while a plan step is already running")


def validate_review_patch_action(proposed_action: AgentAction) -> None:
    if proposed_action.type != AgentActionType.PROPOSE_PATCH:
        raise AgentStateValidationError("review_patch requires a propose_patch action")
    if proposed_action.patch_diff is None or not proposed_action.patch_diff.strip():
        raise AgentStateValidationError("Patch proposal must include a non-empty patch_diff")


def extract_patch_changed_files(patch_diff: str) -> tuple[tuple[str, bool], ...]:
    lines = patch_diff.splitlines()
    pairs: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise AgentStateValidationError("Patch diff must contain matching ---/+++ headers")
            old_path = _normalize_patch_header_path(line[4:].strip())
            new_path = _normalize_patch_header_path(lines[index + 1][4:].strip())
            pairs.append((old_path, new_path))
            index += 2
            continue
        index += 1

    if not pairs:
        raise AgentStateValidationError("Patch diff must contain at least one file header pair")

    changed_files: list[tuple[str, bool]] = []
    for old_path, new_path in pairs:
        deleted = new_path == "/dev/null"
        changed_path = old_path if deleted else new_path
        if changed_path == "/dev/null":
            raise AgentStateValidationError("Patch diff may not use /dev/null for both file headers")
        validate_workspace_relative_path(changed_path)
        changed_files.append((changed_path, deleted))

    return tuple(changed_files)


def validate_workspace_relative_path(path: str) -> None:
    if not path.strip():
        raise AgentStateValidationError("Patch path may not be empty")
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute():
        raise AgentStateValidationError(f"Patch path must be workspace-relative: {path!r}")
    if any(part == ".." for part in pure_path.parts):
        raise AgentStateValidationError(f"Patch path may not escape the workspace: {path!r}")
    if path.startswith("~"):
        raise AgentStateValidationError(f"Patch path may not be home-relative: {path!r}")


def evaluate_loop_guard(session: AgentSession) -> LoopGuardResult:
    validate_session_basic_shape(session)

    if session.iteration_count >= MAX_AGENT_ITERATIONS:
        return LoopGuardResult(
            triggered=True,
            guard_kind="max_iterations",
            reason=f"Iteration limit exceeded ({session.iteration_count} >= {MAX_AGENT_ITERATIONS})",
        )

    if _remaining_context_exhausted(session):
        return LoopGuardResult(
            triggered=True,
            guard_kind="context_overflow",
            reason="Context budget is exhausted",
        )

    if _has_repeated_action(session.action_history):
        return LoopGuardResult(
            triggered=True,
            guard_kind="repeated_action",
            reason="The same action has been repeated too many times",
        )

    if _count_consecutive_failures(session.failure_history, "command") >= MAX_REPEATED_COMMAND_FAILURES:
        return LoopGuardResult(
            triggered=True,
            guard_kind="repeated_command_failures",
            reason="Command failures repeated without recovery",
        )

    if _count_consecutive_failures(session.failure_history, "review_patch") >= MAX_REPEATED_PATCH_REVIEW_FAILURES:
        return LoopGuardResult(
            triggered=True,
            guard_kind="repeated_patch_review_failures",
            reason="Patch review failures repeated without recovery",
        )

    plan = session.current_plan
    if (
        plan is not None
        and session.iteration_count >= MAX_NO_PROGRESS_ITERATIONS
        and len(session.action_history) >= MAX_NO_PROGRESS_ITERATIONS
        and not any(step.status == TaskStatus.SUCCEEDED for step in plan.steps)
        and any(step.status == TaskStatus.PENDING for step in plan.steps)
    ):
        return LoopGuardResult(
            triggered=True,
            guard_kind="no_progress",
            reason="No plan-step progress has been made across recent iterations",
        )

    return LoopGuardResult(triggered=False)


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


def _normalize_patch_header_path(raw_path: str) -> str:
    path = raw_path.split("\t", 1)[0].strip()
    if path in {"/dev/null"}:
        return path
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _remaining_context_exhausted(session: AgentSession) -> bool:
    budget = session.context_budget
    if budget is None:
        return False
    return any(
        remaining is not None and remaining <= 0
        for remaining in (budget.remaining_input_tokens, budget.remaining_output_tokens)
    )


def _has_repeated_action(action_history: Sequence[AgentAction]) -> bool:
    if len(action_history) < MAX_REPEATED_ACTIONS:
        return False
    recent = action_history[-MAX_REPEATED_ACTIONS:]
    signature = _action_signature(recent[0])
    return all(_action_signature(action) == signature for action in recent[1:])


def _action_signature(action: AgentAction) -> tuple[object, ...]:
    return (
        action.type.value,
        action.reason,
        action.target_files,
        action.command_argv,
        action.requested_context,
        action.allow_file_deletions,
    )


def _count_consecutive_failures(failures: Sequence[object], stage: str) -> int:
    count = 0
    for failure in reversed(failures):
        if getattr(failure, "stage", None) != stage:
            break
        count += 1
    return count
