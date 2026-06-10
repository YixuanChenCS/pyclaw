from __future__ import annotations

from collections.abc import Mapping
import re
import shlex
from typing import Iterable
from typing import Sequence

from packages.shared_types import ArtifactRef, RepoContextResult, TaskStatus
from packages.shared_types.ids import RunId, WorkspaceId

from .model_client import ModelClient
from .models import (
    AgentAction,
    AgentActionType,
    AgentContextBudget,
    AgentFailure,
    AgentPlan,
    AgentSession,
    AgentStep,
    PatchReview,
    RunSummary,
)
from .prompts import render_create_plan_prompt
from .service import AgentCoreService
from .validation import (
    AgentStateValidationError,
    evaluate_loop_guard,
    extract_patch_changed_files,
    parse_json_object,
    validate_review_patch_action,
    validate_next_action_session,
    validate_plan_payload,
    validate_session_basic_shape,
)


class LocalAgentCoreService(AgentCoreService):
    """Headless local agent-core skeleton with no runtime or model side effects."""

    _COMMAND_HINT_RE = re.compile(r"`([^`]+)`")

    def __init__(self, *, model_client: ModelClient | None = None) -> None:
        self._model_client = model_client

    @property
    def model_client(self) -> ModelClient | None:
        return self._model_client

    def create_session(
        self,
        *,
        run_id: RunId,
        workspace_id: WorkspaceId,
        user_request: str,
        repo_context: RepoContextResult | None = None,
        current_plan: AgentPlan | None = None,
        prior_artifacts: Sequence[ArtifactRef] = (),
        action_history: Sequence[AgentAction] = (),
        iteration_count: int = 0,
        failure_history: Sequence[AgentFailure] = (),
        warnings: Sequence[str] = (),
        context_budget: AgentContextBudget | None = None,
    ) -> AgentSession:
        session = AgentSession(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request=user_request,
            repo_context=repo_context,
            current_plan=current_plan,
            prior_artifacts=list(prior_artifacts),
            action_history=list(action_history),
            iteration_count=iteration_count,
            failure_history=list(failure_history),
            warnings=list(warnings),
            context_budget=context_budget,
        )
        validate_session_basic_shape(session)
        return session

    async def create_plan(self, session: AgentSession) -> AgentPlan:
        validate_session_basic_shape(session)

        if self._model_client is None:
            raise RuntimeError("LocalAgentCoreService requires a model_client for create_plan")

        prompt = render_create_plan_prompt(session)
        response = await self._model_client.complete_json(prompt)

        if isinstance(response, str):
            payload = parse_json_object(response)
        elif isinstance(response, Mapping):
            payload = response
        else:
            raise TypeError("ModelClient.complete_json must return a JSON string or mapping")

        normalized = validate_plan_payload(payload)
        steps = [
            AgentStep(
                kind=step["kind"],
                description=step["description"],
                target_files=step["target_files"],
                rationale=step["rationale"],
            )
            for step in normalized["steps"]
        ]
        return AgentPlan(
            goal=normalized["goal"],
            steps=steps,
            summary=normalized["summary"],
        )

    async def next_action(self, session: AgentSession) -> AgentAction:
        validate_next_action_session(session)
        guard_result = evaluate_loop_guard(session)
        if guard_result.triggered:
            return AgentAction(
                type=AgentActionType.REQUEST_APPROVAL,
                reason=guard_result.reason or "Loop guard triggered",
                approval_message=guard_result.reason,
                approval_risk_reason=f"Loop guard triggered: {guard_result.guard_kind}",
            )

        plan = session.current_plan
        assert plan is not None

        latest_failure = self._latest_failure(session)
        if latest_failure is not None:
            return AgentAction(
                type=AgentActionType.REQUEST_APPROVAL,
                reason=f"Cannot continue automatically after {latest_failure.stage} failure",
                approval_message=latest_failure.message,
                approval_risk_reason="Previous agent action failed and requires explicit review",
            )

        failed_step = next(
            (
                step
                for step in plan.steps
                if step.status == TaskStatus.FAILED
            ),
            None,
        )
        if failed_step is not None:
            return AgentAction(
                type=AgentActionType.REQUEST_APPROVAL,
                reason=f"Plan step failed: {failed_step.description}",
                target_files=failed_step.target_files,
                approval_message=failed_step.description,
                approval_risk_reason="A prior plan step is marked failed and cannot be skipped",
            )

        next_step = next((step for step in plan.steps if step.status == TaskStatus.PENDING), None)
        if next_step is None:
            return AgentAction(
                type=AgentActionType.COMPLETE,
                reason="Plan has no remaining pending steps",
                summary_text=plan.summary or plan.goal,
            )

        return self._action_for_step(plan, next_step)

    async def review_patch(
        self,
        session: AgentSession,
        proposed_action: AgentAction,
    ) -> PatchReview:
        validate_session_basic_shape(session)
        validate_review_patch_action(proposed_action)

        changed_files = extract_patch_changed_files(proposed_action.patch_diff or "")
        changed_paths = tuple(path for path, _deleted in changed_files)

        if any(deleted for _path, deleted in changed_files) and not proposed_action.allow_file_deletions:
            raise AgentStateValidationError(
                "Patch diff deletes a file but the current action does not allow file deletions"
            )

        if proposed_action.target_files:
            unexpected = sorted(set(changed_paths) - set(proposed_action.target_files))
            if unexpected:
                raise AgentStateValidationError(
                    f"Patch diff references files outside the current action targets: {unexpected!r}"
                )

        return PatchReview(
            accepted=True,
            reason="Patch proposal passed deterministic safety review",
            changed_files=changed_paths,
            patch_diff=proposed_action.patch_diff,
        )

    async def summarize_run(self, session: AgentSession) -> RunSummary:
        validate_session_basic_shape(session)

        plan = session.current_plan
        completed_steps = ()
        unfinished_items = ()
        if plan is not None:
            completed_steps = tuple(
                step.description for step in plan.steps if step.status == TaskStatus.SUCCEEDED
            )
            unfinished_items = tuple(
                step.description
                for step in plan.steps
                if step.status in {TaskStatus.PENDING, TaskStatus.FAILED, TaskStatus.CANCELLED}
            )

        attempted_actions = tuple(action.type.value for action in session.action_history)
        changed_files = tuple(
            self._ordered_unique(
                path
                for action in session.action_history
                if action.type == AgentActionType.PROPOSE_PATCH
                for path in action.target_files
            )
        )
        commands_run = tuple(
            action.command_argv
            for action in session.action_history
            if action.type == AgentActionType.RUN_COMMAND and action.command_argv
        )
        checks_passed = self._checks_passed(session)
        failure_messages = tuple(failure.message for failure in session.failure_history)

        return RunSummary(
            final_status=self._final_status(session),
            completed_steps=completed_steps,
            attempted_actions=attempted_actions,
            changed_files=changed_files,
            commands_run=commands_run,
            checks_passed=checks_passed,
            warnings=tuple(session.warnings),
            unfinished_items=unfinished_items,
            failure_messages=failure_messages,
        )

    def _latest_failure(self, session: AgentSession) -> AgentFailure | None:
        if not session.failure_history:
            return None
        return session.failure_history[-1]

    def _checks_passed(self, session: AgentSession) -> bool | None:
        commands_run = any(action.type == AgentActionType.RUN_COMMAND for action in session.action_history)
        if not commands_run:
            return None
        return not any(failure.stage == "command" for failure in session.failure_history)

    def _final_status(self, session: AgentSession) -> str:
        plan = session.current_plan
        if session.failure_history or (
            plan is not None and any(step.status == TaskStatus.FAILED for step in plan.steps)
        ):
            return "failed"
        if plan is None:
            return "completed" if not session.warnings else "completed_with_warnings"
        if any(step.status in {TaskStatus.PENDING, TaskStatus.RUNNING} for step in plan.steps):
            return "incomplete"
        return "completed_with_warnings" if session.warnings else "completed"

    def _ordered_unique(self, items: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered

    def _action_for_step(self, plan: AgentPlan, step: AgentStep) -> AgentAction:
        kind = step.kind.strip().lower()

        if kind == "inspect":
            return AgentAction(
                type=AgentActionType.ASK_CONTEXT,
                reason=step.description,
                target_files=step.target_files,
                requested_context=step.target_files,
            )

        if kind == "patch":
            return AgentAction(
                type=AgentActionType.PROPOSE_PATCH,
                reason=step.description,
                target_files=step.target_files,
                summary_text=step.rationale,
            )

        if kind == "command":
            return AgentAction(
                type=AgentActionType.RUN_COMMAND,
                reason=step.description,
                target_files=step.target_files,
                command_argv=self._command_for_step(step),
            )

        if kind == "approval":
            return AgentAction(
                type=AgentActionType.REQUEST_APPROVAL,
                reason=step.description,
                target_files=step.target_files,
                approval_message=step.description,
                approval_risk_reason=step.rationale
                or "Plan requires explicit approval before execution continues",
            )

        if kind == "summarize":
            return AgentAction(
                type=AgentActionType.SUMMARIZE,
                reason=step.description,
                summary_text=step.rationale or step.description,
            )

        if kind == "complete":
            return AgentAction(
                type=AgentActionType.COMPLETE,
                reason=step.description,
                summary_text=plan.summary or step.description,
            )

        raise AgentStateValidationError(f"Unsupported plan step kind {kind!r}")

    def _command_for_step(self, step: AgentStep) -> tuple[str, ...]:
        explicit_command = self._explicit_command_hint(step)
        if explicit_command:
            return explicit_command

        lowered_text = " ".join(part.lower() for part in (step.description, step.rationale or ""))
        if step.target_files and all(self._is_unittest_path(path) for path in step.target_files):
            return (
                "python",
                "-m",
                "unittest",
                *(self._path_to_module(path) for path in step.target_files),
            )

        if step.target_files and "check" in lowered_text and all(
            path.endswith(".py") for path in step.target_files
        ):
            return ("python", "-m", "py_compile", *step.target_files)

        if "test" in lowered_text or "unittest" in lowered_text:
            return ("python", "-m", "unittest")

        raise AgentStateValidationError(
            "Command plan steps must provide test targets, python files for checks, or an explicit command hint"
        )

    def _explicit_command_hint(self, step: AgentStep) -> tuple[str, ...]:
        for field in (step.rationale, step.description):
            if not field:
                continue

            marker_index = field.lower().find("command:")
            if marker_index != -1:
                command_text = field[marker_index + len("command:") :].strip()
                if not command_text:
                    raise AgentStateValidationError("Command hint after 'command:' may not be empty")
                return tuple(shlex.split(command_text))

            fenced_match = self._COMMAND_HINT_RE.search(field)
            if fenced_match:
                return tuple(shlex.split(fenced_match.group(1)))

        return ()

    def _is_unittest_path(self, path: str) -> bool:
        return path.startswith("tests/") and path.endswith(".py")

    def _path_to_module(self, path: str) -> str:
        if not path.endswith(".py"):
            raise AgentStateValidationError(f"Expected a Python test module path, got {path!r}")
        return path[:-3].replace("/", ".")
