from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Mapping, Sequence

from packages.shared_types import TaskStatus
from packages.shared_types import ErrorCodeContractError
from services.execution_runtime.patch import (
    apply_file_patch_to_text,
    parse_unified_diff,
    validate_hunk_line_counts,
)

from .models import AgentAction, AgentActionType, AgentSession, LoopGuardResult

VALID_PLAN_STEP_KINDS = frozenset(
    {"inspect", "patch", "command", "approval", "summarize", "complete"}
)
MAX_AGENT_PLAN_STEPS = 8
MAX_PATCH_EDIT_BLOCKS = 16
MAX_AGENT_ITERATIONS = 12
MAX_REPEATED_ACTIONS = 3
MAX_REPEATED_CONTEXT_REQUESTS = 3
MAX_REPEATED_COMMAND_FAILURES = 2
MAX_REPEATED_PATCH_REVIEW_FAILURES = 2
MAX_NO_PROGRESS_ITERATIONS = 3


class AgentCoreValidationError(ValueError):
    """Base validation error for agent-core structured outputs."""


class AgentPlanValidationError(AgentCoreValidationError):
    """Raised when a model-produced plan is malformed or unsupported."""


class AgentPatchValidationError(AgentCoreValidationError):
    """Raised when a model-produced patch payload is malformed or unsupported."""


class AgentPatchGenerationError(AgentPatchValidationError):
    """Raised when structured patch intent cannot be converted into a deterministic patch."""

    def __init__(self, failure_code: str, message: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


class AgentCommandValidationError(AgentCoreValidationError):
    """Raised when a model-produced command payload is malformed or unsupported."""


class AgentStateValidationError(AgentCoreValidationError):
    """Raised when session state is inconsistent for deterministic action selection."""


def validate_action_type(action_type: AgentActionType | str) -> AgentActionType:
    if isinstance(action_type, AgentActionType):
        return action_type

    try:
        return AgentActionType(action_type)
    except ValueError as exc:
        raise ValueError(f"Unsupported agent action type: {action_type!r}") from exc


def validate_action_for_dispatch(action: AgentAction) -> None:
    action_type = validate_action_type(action.type)

    if not isinstance(action.reason, str) or not action.reason.strip():
        raise AgentStateValidationError("AgentAction.reason must be a non-empty string")
    if action.step_id is not None and (not isinstance(action.step_id, str) or not action.step_id.strip()):
        raise AgentStateValidationError("AgentAction.step_id must be a non-empty string when provided")
    if action.action_id is not None and (not isinstance(action.action_id, str) or not action.action_id.strip()):
        raise AgentStateValidationError("AgentAction.action_id must be a non-empty string when provided")

    if action_type == AgentActionType.RUN_COMMAND and not action.command_argv:
        raise AgentStateValidationError("RUN_COMMAND actions must include command_argv")

    if action_type == AgentActionType.PROPOSE_PATCH:
        if action.patch_diff is None or not action.patch_diff.strip():
            raise AgentStateValidationError("PROPOSE_PATCH actions must include patch_diff")

    if action_type == AgentActionType.ASK_CONTEXT and not action.requested_context:
        raise AgentStateValidationError("ASK_CONTEXT actions must include requested_context")


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
    seen_step_ids: set[str] = set()
    for index, step in enumerate(plan.steps, start=1):
        if step.step_id is not None:
            if not isinstance(step.step_id, str) or not step.step_id.strip():
                raise AgentStateValidationError(
                    f"Plan step {index} step_id must be a non-empty string when provided"
                )
            if step.step_id in seen_step_ids:
                raise AgentStateValidationError(f"Plan step {index} reuses duplicate step_id {step.step_id!r}")
            seen_step_ids.add(step.step_id)

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
    _validate_workspace_relative_path_with(
        path,
        error_type=AgentStateValidationError,
        empty_message="Patch path may not be empty",
        absolute_message=lambda value: f"Patch path must be workspace-relative: {value!r}",
        escape_message=lambda value: f"Patch path may not escape the workspace: {value!r}",
        home_message=lambda value: f"Patch path may not be home-relative: {value!r}",
    )


def _validate_workspace_relative_path_with(
    path: str,
    *,
    error_type: type[AgentCoreValidationError],
    empty_message: str,
    absolute_message,
    escape_message,
    home_message,
) -> None:
    if not path.strip():
        raise error_type(empty_message)
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute():
        raise error_type(absolute_message(path))
    if any(part == ".." for part in pure_path.parts):
        raise error_type(escape_message(path))
    if path.startswith("~"):
        raise error_type(home_message(path))


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

    if _count_consecutive_context_requests(session.action_history) >= MAX_REPEATED_CONTEXT_REQUESTS:
        return LoopGuardResult(
            triggered=True,
            guard_kind="repeated_context_requests",
            reason="Context requests repeated without incorporating new context",
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


def parse_json_object(
    text: str,
    *,
    malformed_message: str = "Model returned malformed JSON",
    object_message: str = "Response must be a JSON object",
    error_type: type[AgentCoreValidationError] = AgentPlanValidationError,
) -> Mapping[str, object]:
    direct_error: json.JSONDecodeError | None = None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        direct_error = exc
    else:
        if not isinstance(payload, dict):
            raise error_type(object_message)
        return payload

    fenced_candidates = _extract_fenced_json_candidates(text)
    if len(fenced_candidates) > 1:
        raise error_type(f"{malformed_message}: multiple fenced JSON objects found")
    if len(fenced_candidates) == 1:
        return _parse_json_candidate(
            fenced_candidates[0],
            malformed_message=malformed_message,
            object_message=object_message,
            error_type=error_type,
        )

    top_level_candidates = _extract_top_level_json_objects(text)
    if len(top_level_candidates) != 1:
        detail = "no JSON object found" if not top_level_candidates else "multiple JSON objects found"
        raise error_type(f"{malformed_message}: {detail}")
    return _parse_json_candidate(
        top_level_candidates[0],
        malformed_message=malformed_message,
        object_message=object_message,
        error_type=error_type,
        cause=direct_error,
    )


def _parse_json_candidate(
    candidate: str,
    *,
    malformed_message: str,
    object_message: str,
    error_type: type[AgentCoreValidationError],
    cause: Exception | None = None,
) -> Mapping[str, object]:
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise error_type(malformed_message) from (cause or exc)

    if not isinstance(payload, dict):
        raise error_type(object_message)
    return payload


def _extract_fenced_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip().lower()
        if not line.startswith("```json"):
            index += 1
            continue
        block_lines: list[str] = []
        index += 1
        while index < len(lines) and not lines[index].strip().startswith("```"):
            block_lines.append(lines[index])
            index += 1
        if index >= len(lines):
            break
        candidates.append("\n".join(block_lines).strip())
        index += 1
    return [candidate for candidate in candidates if candidate]


def _extract_top_level_json_objects(text: str) -> list[str]:
    candidates: list[str] = []
    start_index: int | None = None
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == "{":
            if depth == 0:
                start_index = index
            depth += 1
            continue

        if char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start_index is not None:
                candidates.append(text[start_index : index + 1].strip())
                start_index = None

    return candidates


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


def validate_patch_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return validate_patch_intent_payload(payload)


def validate_patch_intent_payload(
    payload: Mapping[str, object],
    *,
    allowed_paths: Sequence[str] = (),
) -> dict[str, str]:
    path_value = payload.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise AgentPatchValidationError("Patch response path must be a non-empty string")

    search_value = payload.get("search")
    if not isinstance(search_value, str):
        raise AgentPatchValidationError("Patch response search must be a string")

    replace_value = payload.get("replace")
    if not isinstance(replace_value, str):
        raise AgentPatchValidationError("Patch response replace must be a string")

    path = path_value.strip()
    _validate_workspace_relative_path_with(
        path,
        error_type=AgentPatchValidationError,
        empty_message="Patch response path must be a non-empty string",
        absolute_message=lambda value: f"Patch response path must be workspace-relative: {value!r}",
        escape_message=lambda value: f"Patch response path may not escape the workspace: {value!r}",
        home_message=lambda value: f"Patch response path may not be home-relative: {value!r}",
    )

    if allowed_paths and path not in allowed_paths:
        raise AgentPatchValidationError(
            f"Patch response path {path!r} must be one of the current action target files"
        )

    return {
        "path": path,
        "search": search_value,
        "replace": replace_value,
    }


def validate_command_payload(payload: Mapping[str, object]) -> dict[str, object]:
    command_argv_value = payload.get("command_argv")
    if not isinstance(command_argv_value, list) or not command_argv_value:
        raise AgentCommandValidationError("Command response command_argv must be a non-empty list")

    normalized_argv: list[str] = []
    for index, item in enumerate(command_argv_value, start=1):
        if not isinstance(item, str) or not item.strip():
            raise AgentCommandValidationError(
                f"Command response command_argv[{index}] must be a non-empty string"
            )
        normalized_argv.append(item)

    cwd_value = payload.get("cwd")
    if cwd_value is None:
        cwd = None
    elif not isinstance(cwd_value, str) or not cwd_value.strip():
        raise AgentCommandValidationError("Command response cwd must be a non-empty string when provided")
    else:
        cwd = cwd_value

    return {
        "command_argv": tuple(normalized_argv),
        "cwd": cwd,
    }


def _normalize_patch_header_path(raw_path: str) -> str:
    path = raw_path.split("\t", 1)[0].strip()
    if path in {"/dev/null"}:
        return path
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def validate_patch_diff_against_session(
    session: AgentSession,
    action: AgentAction,
) -> tuple[str, ...]:
    patch_diff = action.patch_diff or ""
    try:
        file_patches = parse_unified_diff(patch_diff)
    except ErrorCodeContractError as exc:
        raise AgentStateValidationError(str(exc)) from exc

    if not file_patches:
        raise AgentStateValidationError("Patch diff must contain at least one file diff")

    for file_patch in file_patches:
        try:
            validate_hunk_line_counts(file_patch)
        except ErrorCodeContractError as exc:
            raise AgentStateValidationError(str(exc)) from exc

    changed_files = extract_patch_changed_files(patch_diff)
    content_by_path = _repo_context_file_contents(session)
    for file_patch in file_patches:
        target_path = file_patch.new_path if file_patch.new_path != "/dev/null" else file_patch.old_path
        if file_patch.old_path == "/dev/null":
            current_text = ""
        else:
            current_text = content_by_path.get(target_path)
            if current_text is None:
                continue
        try:
            apply_file_patch_to_text(file_patch, current_text)
        except ErrorCodeContractError as exc:
            raise AgentStateValidationError(str(exc)) from exc

    return tuple(path for path, _deleted in changed_files)


def _repo_context_file_contents(session: AgentSession) -> dict[str, str]:
    repo_context = session.repo_context
    if repo_context is None:
        return {}
    return {
        item.path: item.content
        for item in repo_context.file_summaries
        if item.content is not None
    }


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


def _count_consecutive_context_requests(action_history: Sequence[AgentAction]) -> int:
    count = 0
    for action in reversed(action_history):
        if action.type != AgentActionType.ASK_CONTEXT:
            break
        count += 1
    return count
