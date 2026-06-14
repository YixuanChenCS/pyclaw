from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterable
from typing import Sequence

from packages.shared_types import ArtifactRef, RepoContextResult, TaskStatus
from packages.shared_types.ids import RunId, WorkspaceId

from .model_client import ModelClient
from .edit_blocks import (
    SearchReplaceApplicationError,
    SearchReplaceEdit,
    apply_search_replace_edits,
    build_unified_diff,
)
from .models import (
    AgentAction,
    AgentActionType,
    AgentContextBudget,
    AgentFailure,
    AgentPlan,
    AgentSession,
    AgentSessionPhase,
    AgentStep,
    AgentVerification,
    PatchReview,
    RunSummary,
)
from .prompts import render_create_plan_prompt
from .prompts import render_generate_command_prompt
from .prompts import render_generate_patch_prompt
from .reducer import ensure_action_id
from .session_store import AgentSessionStore
from .service import AgentCoreService
from .validation import (
    AgentCommandValidationError,
    AgentPatchGenerationError,
    AgentPatchValidationError,
    AgentStateValidationError,
    evaluate_loop_guard,
    extract_patch_changed_files,
    parse_json_object,
    reference_paths_from_session,
    validate_command_payload,
    validate_patch_intent_payload,
    validate_patch_diff_against_session,
    validate_review_patch_action,
    validate_next_action_session,
    validate_plan_payload,
    validate_session_basic_shape,
)

_VERIFICATION_IGNORED_SEGMENTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".cache",
        "node_modules",
        "vendor",
        "dist",
        "build",
    }
)
_VERIFICATION_IGNORED_SUFFIXES = (
    ".generated.py",
    "_generated.py",
    "_pb2.py",
    "_pb2_grpc.py",
)
DEFAULT_TARGETED_TEST_ROOTS = ("tests/unit",)
DEFAULT_TARGETED_TEST_COMMAND_PREFIX = ("python", "-m", "pytest")
MAX_VERIFICATION_ATTEMPTS_PER_SIGNATURE = 3


class LocalAgentCoreService(AgentCoreService):
    """Headless local agent-core skeleton with no runtime or model side effects."""

    def __init__(
        self,
        *,
        model_client: ModelClient | None = None,
        session_store: AgentSessionStore | None = None,
        targeted_test_roots: Sequence[str] = DEFAULT_TARGETED_TEST_ROOTS,
        targeted_test_command_prefix: Sequence[str] = DEFAULT_TARGETED_TEST_COMMAND_PREFIX,
        fallback_test_command: Sequence[str] = (),
    ) -> None:
        self._model_client = model_client
        self._session_store = session_store
        self._targeted_test_roots = tuple(targeted_test_roots)
        self._targeted_test_command_prefix = tuple(targeted_test_command_prefix)
        self._fallback_test_command = tuple(fallback_test_command)

    @property
    def model_client(self) -> ModelClient | None:
        return self._model_client

    def create_session(
        self,
        *,
        run_id: RunId,
        workspace_id: WorkspaceId,
        user_request: str,
        phase: AgentSessionPhase | None = None,
        repo_context: RepoContextResult | None = None,
        current_plan: AgentPlan | None = None,
        prior_artifacts: Sequence[ArtifactRef] = (),
        action_history: Sequence[AgentAction] = (),
        pending_action: AgentAction | None = None,
        pending_approval_id: str | None = None,
        completed_action_ids: Sequence[str] = (),
        iteration_count: int = 0,
        failure_history: Sequence[AgentFailure] = (),
        verification_history: Sequence[AgentVerification] = (),
        warnings: Sequence[str] = (),
        context_budget: AgentContextBudget | None = None,
    ) -> AgentSession:
        session = AgentSession(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request=user_request,
            phase=phase or self._initial_phase(current_plan),
            repo_context=repo_context,
            current_plan=self._normalize_plan_step_ids(current_plan),
            prior_artifacts=list(prior_artifacts),
            action_history=list(action_history),
            pending_action=pending_action,
            pending_approval_id=pending_approval_id,
            completed_action_ids=list(completed_action_ids),
            iteration_count=iteration_count,
            failure_history=list(failure_history),
            verification_history=list(verification_history),
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
                step_id=f"step_{index}",
                kind=step["kind"],
                description=step["description"],
                target_files=step["target_files"],
                rationale=step["rationale"],
            )
            for index, step in enumerate(normalized["steps"], start=1)
        ]
        plan = AgentPlan(
            goal=normalized["goal"],
            steps=steps,
            summary=normalized["summary"],
        )

        if self._session_store is not None:
            await self._session_store.save_agent_session(
                replace(session, current_plan=plan, phase=AgentSessionPhase.READY)
            )

        return plan

    async def next_action(self, session: AgentSession) -> AgentAction:
        validate_next_action_session(session)
        guard_result = evaluate_loop_guard(session)
        if guard_result.triggered:
            return AgentAction(
                type=AgentActionType.REQUEST_APPROVAL,
                reason=guard_result.reason or "Loop guard triggered",
                approval_message=guard_result.reason,
                approval_risk_reason=f"Loop guard triggered: {guard_result.guard_kind}",
                action_id=self._action_id(session, AgentActionType.REQUEST_APPROVAL),
            )

        plan = session.current_plan
        assert plan is not None

        latest_failure = self._latest_failure(session)
        recovery_action = self._action_for_retryable_failure(session, latest_failure)
        if recovery_action is not None:
            return recovery_action

        if latest_failure is not None:
            return AgentAction(
                type=AgentActionType.REQUEST_APPROVAL,
                reason=f"Cannot continue automatically after {latest_failure.stage} failure",
                approval_message=latest_failure.message,
                approval_risk_reason="Previous agent action failed and requires explicit review",
                action_id=self._action_id(session, AgentActionType.REQUEST_APPROVAL),
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
                action_id=self._action_id(session, AgentActionType.REQUEST_APPROVAL, failed_step.step_id),
            )

        next_step = next((step for step in plan.steps if step.status == TaskStatus.PENDING), None)
        if next_step is None:
            return AgentAction(
                type=AgentActionType.COMPLETE,
                reason="Plan has no remaining pending steps",
                summary_text=plan.summary or plan.goal,
                action_id=self._action_id(session, AgentActionType.COMPLETE),
            )

        return self._action_for_step(session, plan, next_step)

    async def generate_command(
        self,
        session: AgentSession,
        proposed_action: AgentAction,
    ) -> AgentAction:
        validate_session_basic_shape(session)
        if proposed_action.type != AgentActionType.RUN_COMMAND:
            raise AgentStateValidationError("generate_command requires a run_command action")
        if self._model_client is None:
            raise RuntimeError("LocalAgentCoreService requires a model_client for generate_command")
        if proposed_action.command_argv:
            return proposed_action

        prompt = render_generate_command_prompt(session, proposed_action)
        response = await self._model_client.complete_json(prompt)

        if isinstance(response, str):
            payload = parse_json_object(
                response,
                malformed_message="Model returned malformed command JSON",
                object_message="Command response must be a JSON object",
                error_type=AgentCommandValidationError,
            )
        elif isinstance(response, Mapping):
            payload = response
        else:
            raise TypeError("ModelClient.complete_json must return a JSON string or mapping")

        normalized = validate_command_payload(payload)
        return replace(
            proposed_action,
            command_argv=normalized["command_argv"],
            cwd=normalized["cwd"],
        )

    async def generate_patch(
        self,
        session: AgentSession,
        proposed_action: AgentAction,
    ) -> AgentAction:
        validate_session_basic_shape(session)
        if proposed_action.type != AgentActionType.PROPOSE_PATCH:
            raise AgentStateValidationError("generate_patch requires a propose_patch action")
        if self._model_client is None:
            raise RuntimeError("LocalAgentCoreService requires a model_client for generate_patch")
        if proposed_action.patch_diff is not None and proposed_action.patch_diff.strip():
            return proposed_action

        prompt = render_generate_patch_prompt(session, proposed_action)
        response = await self._model_client.complete_json(prompt)

        if isinstance(response, str):
            try:
                payload = parse_json_object(
                    response,
                    malformed_message="Model returned malformed patch JSON",
                    object_message="Patch response must be a JSON object",
                    error_type=AgentPatchValidationError,
                )
            except AgentPatchValidationError as exc:
                raise AgentPatchGenerationError("json_parse_failed", str(exc)) from exc
        elif isinstance(response, Mapping):
            payload = response
        else:
            raise TypeError("ModelClient.complete_json must return a JSON string or mapping")

        try:
            normalized = validate_patch_intent_payload(
                payload,
                allowed_paths=proposed_action.target_files,
            )
        except AgentPatchValidationError as exc:
            raise AgentPatchGenerationError("schema_invalid", str(exc)) from exc

        patch_diff = self._build_patch_diff_from_search_replace_intent(
            session,
            proposed_action,
            normalized["path"],
            normalized["search"],
            normalized["replace"],
        )
        return replace(
            proposed_action,
            patch_diff=str(patch_diff),
            allow_file_deletions=False,
        )

    async def review_patch(
        self,
        session: AgentSession,
        proposed_action: AgentAction,
    ) -> PatchReview:
        validate_session_basic_shape(session)
        validate_review_patch_action(proposed_action)

        changed_paths = validate_patch_diff_against_session(session, proposed_action)
        changed_files = extract_patch_changed_files(proposed_action.patch_diff or "")

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

    def _build_patch_diff_from_search_replace_intent(
        self,
        session: AgentSession,
        proposed_action: AgentAction,
        path: str,
        search: str,
        replace_text: str,
    ) -> str:
        if not proposed_action.target_files:
            raise AgentPatchGenerationError(
                "schema_invalid",
                "Patch generation requires target_files for structured edit intent",
            )

        original_contents = self._repo_context_file_contents_for_targets(session, proposed_action.target_files)
        if path not in proposed_action.target_files:
            raise AgentPatchGenerationError(
                "schema_invalid",
                f"Patch response referenced files outside the current action targets: {path!r}",
            )

        try:
            updated_contents = apply_search_replace_edits(
                original_contents,
                (
                    SearchReplaceEdit(
                        path=path,
                        search=search,
                        replace=replace_text,
                    ),
                ),
            )
        except SearchReplaceApplicationError as exc:
            raise AgentPatchGenerationError(exc.failure_code, str(exc)) from exc

        patch_diff = build_unified_diff(
            path=path,
            before=original_contents[path],
            after=updated_contents[path],
        )
        if not patch_diff.strip():
            raise AgentPatchGenerationError(
                "schema_invalid",
                "Structured edit intent did not produce any file changes",
            )
        return patch_diff

    def _repo_context_file_contents_for_targets(
        self,
        session: AgentSession,
        target_files: tuple[str, ...],
    ) -> dict[str, str]:
        repo_context = session.repo_context
        if repo_context is None:
            raise AgentPatchValidationError("Patch generation requires repo_context with target file contents")

        summaries_by_path = {item.path: item for item in repo_context.file_summaries}
        missing_paths = [path for path in target_files if path not in summaries_by_path]
        if missing_paths:
            raise AgentPatchValidationError(
                f"repo_context is missing target file summaries: {missing_paths!r}"
            )

        contents: dict[str, str] = {}
        missing_content_paths: list[str] = []
        for path in target_files:
            content = summaries_by_path[path].content
            if content is None:
                missing_content_paths.append(path)
                continue
            contents[path] = content

        if missing_content_paths:
            raise AgentPatchValidationError(
                f"repo_context is missing full target file contents: {missing_content_paths!r}"
            )

        return contents

    def plan_patch_verification(
        self,
        session: AgentSession,
        *,
        changed_files: tuple[str, ...],
        deleted_files: tuple[str, ...] = (),
        workspace_root: str | None = None,
    ) -> tuple[AgentVerification, ...]:
        repo_context = session.repo_context

        deleted = set(deleted_files)
        eligible_python_files = tuple(
            self._ordered_unique(
                path
                for path in changed_files
                if path not in deleted and self._is_verifiable_python_path(path)
            )
        )
        if not eligible_python_files:
            return ()

        verification_steps = [
            AgentVerification(
                kind="py_compile",
                verification_level="syntax_only",
                command_argv=("python", "-m", "py_compile", *eligible_python_files),
                changed_files=eligible_python_files,
            )
        ]

        existing_paths = self._known_workspace_paths(repo_context, workspace_root=workspace_root)
        matched_test_files = self._matching_targeted_test_files(
            eligible_python_files,
            existing_paths=existing_paths,
        )
        if matched_test_files:
            verification_steps.extend(
                AgentVerification(
                    kind="targeted_pytest",
                    verification_level="targeted_tests_passed",
                    command_argv=(*self._targeted_test_command_prefix, test_file),
                    changed_files=eligible_python_files,
                )
                for test_file in matched_test_files
            )
            return tuple(verification_steps)

        if self._fallback_test_command:
            verification_steps.append(
                AgentVerification(
                    kind="fallback_pytest",
                    verification_level="fallback_tests_passed",
                    command_argv=self._fallback_test_command,
                    changed_files=eligible_python_files,
                )
            )
            return tuple(verification_steps)

        verification_steps.append(
            AgentVerification(
                kind="functional_verification_missing",
                verification_level="functional_verification_missing",
                changed_files=eligible_python_files,
            )
        )
        return tuple(verification_steps)

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

    def _action_for_retryable_failure(
        self,
        session: AgentSession,
        latest_failure: AgentFailure | None,
    ) -> AgentAction | None:
        if latest_failure is None or not latest_failure.retryable:
            return None
        if latest_failure.stage != "command":
            return None

        latest_action = session.action_history[-1] if session.action_history else None
        if latest_action is None:
            return None

        latest_verification = self._latest_verification(session)
        if (
            latest_action.type == AgentActionType.PROPOSE_PATCH
            and latest_verification is not None
            and latest_verification.trigger_action_id == latest_action.action_id
            and latest_verification.exit_code not in (None, 0)
        ):
            signature = latest_verification.failure_signature
            if (
                signature is not None
                and self._verification_attempt_count(session, signature) >= MAX_VERIFICATION_ATTEMPTS_PER_SIGNATURE
            ):
                return ensure_action_id(
                    session,
                    AgentAction(
                        type=AgentActionType.REQUEST_APPROVAL,
                        reason="Automatic verification failed repeatedly with the same signature",
                        approval_message=latest_failure.message,
                        approval_risk_reason="Automatic verification retried the same failure signature too many times",
                    ),
                )

            target_files = self._repair_target_files_for_verification(session, latest_verification)
            if not target_files:
                return None
            return ensure_action_id(
                session,
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Fix the issues found by automatic post-patch verification",
                    target_files=target_files,
                    summary_text=latest_failure.message,
                ),
            )

        if latest_action.type == AgentActionType.RUN_COMMAND:
            target_files = self._repair_target_files(session, latest_action)
            if not target_files:
                return None
            return ensure_action_id(
                session,
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Fix the issues found by the failed verification command",
                    target_files=target_files,
                    summary_text=latest_failure.message,
                ),
            )

        if latest_action.type == AgentActionType.PROPOSE_PATCH:
            failed_command = self._latest_command_action(session)
            if failed_command is None or not failed_command.command_argv:
                return None
            return ensure_action_id(
                session,
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Re-run the failed verification command after applying a fix",
                    step_id=failed_command.step_id,
                    target_files=failed_command.target_files,
                    command_argv=failed_command.command_argv,
                    cwd=failed_command.cwd,
                    summary_text=latest_failure.message,
                ),
            )

        return None

    def _checks_passed(self, session: AgentSession) -> bool | None:
        commands_run = any(action.type == AgentActionType.RUN_COMMAND for action in session.action_history)
        if not commands_run and session.verification_history:
            commands_run = True
        if not commands_run:
            return None
        return not any(failure.stage == "command" for failure in session.failure_history)

    def _repair_target_files(
        self,
        session: AgentSession,
        failed_command: AgentAction,
    ) -> tuple[str, ...]:
        repo_context = session.repo_context
        if repo_context is not None and repo_context.file_summaries:
            paths = tuple(item.path for item in repo_context.file_summaries if item.path)
            if paths:
                return paths

        if failed_command.target_files:
            return failed_command.target_files

        if repo_context is not None and repo_context.dependency_hints:
            return tuple(repo_context.dependency_hints)

        return ()

    def _repair_target_files_for_verification(
        self,
        session: AgentSession,
        verification: AgentVerification,
    ) -> tuple[str, ...]:
        repo_context = session.repo_context
        if repo_context is not None and repo_context.file_summaries:
            paths = tuple(item.path for item in repo_context.file_summaries if item.path)
            if paths:
                return paths
        return verification.changed_files

    def _latest_command_action(self, session: AgentSession) -> AgentAction | None:
        for action in reversed(session.action_history):
            if action.type == AgentActionType.RUN_COMMAND:
                return action
        return None

    def _latest_verification(self, session: AgentSession) -> AgentVerification | None:
        if not session.verification_history:
            return None
        return session.verification_history[-1]

    def _verification_attempt_count(
        self,
        session: AgentSession,
        failure_signature: str,
    ) -> int:
        return sum(
            1
            for item in session.verification_history
            if item.failure_signature == failure_signature and item.exit_code not in (None, 0)
        )

    def _is_verifiable_python_path(self, path: str) -> bool:
        normalized = PurePosixPath(path)
        if normalized.suffix != ".py":
            return False
        if any(part in _VERIFICATION_IGNORED_SEGMENTS for part in normalized.parts):
            return False
        name = normalized.name
        if name.endswith(_VERIFICATION_IGNORED_SUFFIXES):
            return False
        return True

    def _known_workspace_paths(
        self,
        repo_context: RepoContextResult | None,
        *,
        workspace_root: str | None,
    ) -> set[str]:
        known_paths = {
            item.path
            for item in repo_context.file_summaries
        } if repo_context is not None else set()
        if workspace_root is None:
            return known_paths

        root = Path(workspace_root)
        for test_root in self._targeted_test_roots:
            test_root_path = root / test_root
            if not test_root_path.exists():
                continue
            for file_path in test_root_path.rglob("test_*.py"):
                try:
                    relative = file_path.relative_to(root)
                except ValueError:
                    continue
                known_paths.add(relative.as_posix())
        return known_paths

    def _matching_targeted_test_files(
        self,
        changed_files: tuple[str, ...],
        *,
        existing_paths: set[str],
    ) -> tuple[str, ...]:
        matched: list[str] = []
        seen: set[str] = set()
        for changed_file in changed_files:
            for candidate in self._targeted_test_candidates(changed_file):
                if candidate not in existing_paths or candidate in seen:
                    continue
                seen.add(candidate)
                matched.append(candidate)
        return tuple(matched)

    def _targeted_test_candidates(self, path: str) -> tuple[str, ...]:
        normalized = PurePosixPath(path)
        if normalized.name.startswith("test_") and normalized.suffix == ".py":
            return (normalized.as_posix(),)

        logical_parts = normalized.with_suffix("").parts
        if logical_parts and logical_parts[0] in {"services", "packages", "apps"}:
            logical_parts = logical_parts[1:]
        if not logical_parts:
            return ()

        stem_candidates = [
            "_".join(logical_parts),
            "_".join(logical_parts[-2:]) if len(logical_parts) >= 2 else "",
            logical_parts[-1],
        ]
        candidates: list[str] = []
        for test_root in self._targeted_test_roots:
            for stem in stem_candidates:
                if not stem:
                    continue
                candidate = f"{test_root}/test_{stem}.py"
                if candidate not in candidates:
                    candidates.append(candidate)
        return tuple(candidates)

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

    def _action_for_step(self, session: AgentSession, plan: AgentPlan, step: AgentStep) -> AgentAction:
        kind = step.kind.strip().lower()

        if kind == "inspect":
            return ensure_action_id(session, AgentAction(
                type=AgentActionType.ASK_CONTEXT,
                reason=step.description,
                step_id=step.step_id,
                target_files=step.target_files,
                requested_context=step.target_files,
            ))

        if kind == "patch":
            return ensure_action_id(session, AgentAction(
                type=AgentActionType.PROPOSE_PATCH,
                reason=step.description,
                step_id=step.step_id,
                target_files=self._editable_target_files(session, step.target_files),
                summary_text=step.rationale,
            ))

        if kind == "command":
            return ensure_action_id(session, AgentAction(
                type=AgentActionType.RUN_COMMAND,
                reason=step.description,
                step_id=step.step_id,
                target_files=step.target_files,
                summary_text=step.rationale,
            ))

        if kind == "approval":
            return ensure_action_id(session, AgentAction(
                type=AgentActionType.REQUEST_APPROVAL,
                reason=step.description,
                step_id=step.step_id,
                target_files=step.target_files,
                approval_message=step.description,
                approval_risk_reason=step.rationale
                or "Plan requires explicit approval before execution continues",
            ))

        if kind == "summarize":
            return ensure_action_id(session, AgentAction(
                type=AgentActionType.SUMMARIZE,
                reason=step.description,
                step_id=step.step_id,
                summary_text=step.rationale or step.description,
            ))

        if kind == "complete":
            return ensure_action_id(session, AgentAction(
                type=AgentActionType.COMPLETE,
                reason=step.description,
                step_id=step.step_id,
                summary_text=plan.summary or step.description,
            ))

        raise AgentStateValidationError(f"Unsupported plan step kind {kind!r}")

    def _editable_target_files(
        self,
        session: AgentSession,
        target_files: tuple[str, ...],
    ) -> tuple[str, ...]:
        reference_paths = reference_paths_from_session(session)
        return tuple(
            path
            for path in target_files
            if PurePosixPath(path.replace("\\", "/")).as_posix() not in reference_paths
        )

    def _normalize_plan_step_ids(self, plan: AgentPlan | None) -> AgentPlan | None:
        if plan is None:
            return None

        normalized_steps: list[AgentStep] = []
        changed = False
        for index, step in enumerate(plan.steps, start=1):
            step_id = step.step_id or f"step_{index}"
            if step.step_id != step_id:
                changed = True
                normalized_steps.append(replace(step, step_id=step_id))
            else:
                normalized_steps.append(step)

        if not changed:
            return plan

        return replace(plan, steps=normalized_steps)

    def _action_id(
        self,
        session: AgentSession,
        action_type: AgentActionType,
        step_id: str | None = None,
    ) -> str:
        step_scope = step_id or action_type.value
        return f"action_{session.iteration_count + 1}_{action_type.value}_{step_scope}"

    def _initial_phase(self, current_plan: AgentPlan | None) -> AgentSessionPhase:
        return AgentSessionPhase.READY if current_plan is not None else AgentSessionPhase.PLANNING
