from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import re

from packages.shared_types import (
    ApprovalDecision,
    ApprovalRequest,
    ArtifactRef,
    CommandRequest,
    CommandResult,
    RepoContextRequest,
    RepoStore,
    PatchProposal,
    RecoveryStatus,
    RunResult,
    RunStatus,
    TaskStatus,
    new_task_id,
)
from services.execution_runtime.service import ExecutionRuntimeService
from services.repo_intelligence.service import RepoIntelligenceService

from .models import AgentAction, AgentActionType, AgentRunOutcome, AgentSession, AgentSessionPhase, AgentVerification
from .post_apply import PostApplyValidationFailure, build_post_apply_candidates, run_post_apply_validators
from .reducer import (
    clear_pending_action,
    clear_pending_approval,
    clear_retryable_failures,
    ensure_action_id,
    has_completed_action,
    record_action_success,
    record_failure,
    record_retryable_action_failure,
    record_retryable_failure,
    record_selected_action,
    set_pending_approval,
)
from .session_store import AgentSessionStore
from .service import AgentCoreService
from .validation import AgentStateValidationError, extract_patch_changed_files, validate_action_for_dispatch

MAX_PATCH_RETRIES = 2
RETRYABLE_PATCH_FAILURE_CODES = frozenset(
    {
        "json_parse_failed",
        "schema_invalid",
        "search_not_found",
        "ambiguous_search",
        "post_apply_validation_failed",
        "read_only_reference_modified",
    }
)
_FAILURE_QUERY_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_FAILURE_QUERY_STOPWORDS = frozenset(
    {
        "assertionerror",
        "error",
        "errors",
        "exception",
        "failed",
        "failure",
        "traceback",
        "tests",
        "test",
        "python",
        "line",
        "file",
        "warning",
    }
)


class AgentCoreCoordinator:
    """Thin orchestration layer that dispatches validated agent actions to the runtime."""

    _CLAIM_LEASE_SECONDS = 30
    _CLAIM_WORKER_ID = "agent-core-coordinator"

    def __init__(
        self,
        *,
        agent_core: AgentCoreService,
        execution_runtime: ExecutionRuntimeService,
        session_store: AgentSessionStore | None = None,
        repo_intelligence: RepoIntelligenceService | None = None,
        repo_store: RepoStore | None = None,
    ) -> None:
        self._agent_core = agent_core
        self._execution_runtime = execution_runtime
        self._session_store = session_store
        self._repo_intelligence = repo_intelligence
        self._repo_store = repo_store

    async def run(self, session: AgentSession) -> AgentRunOutcome:
        current_session = session
        applied_artifacts: list[ArtifactRef] = list(session.prior_artifacts)

        while True:
            if current_session.pending_action is not None:
                identified_pending = ensure_action_id(current_session, current_session.pending_action)
                if identified_pending is not current_session.pending_action:
                    current_session = replace(current_session, pending_action=identified_pending)
                    await self._persist_session(current_session)

            if current_session.pending_approval_id is not None:
                return AgentRunOutcome(
                    status="approval_requested",
                    session=current_session,
                    last_action=current_session.pending_action,
                    approval_id=current_session.pending_approval_id,
                    applied_artifacts=tuple(applied_artifacts),
                )

            if has_completed_action(current_session, current_session.pending_action):
                current_session = clear_pending_action(
                    current_session,
                    action=current_session.pending_action,
                    clear_pending_approval=True,
                )
                await self._persist_session(current_session)
                continue

            current_session, action = await self._select_action(current_session)
            if action.type not in {AgentActionType.PROPOSE_PATCH, AgentActionType.RUN_COMMAND} or (
                action.type == AgentActionType.PROPOSE_PATCH
                and action.patch_diff is not None
                and action.patch_diff.strip()
            ) or (
                action.type == AgentActionType.RUN_COMMAND
                and action.command_argv
            ):
                validate_action_for_dispatch(action)

            if action.type == AgentActionType.ASK_CONTEXT:
                return AgentRunOutcome(
                    status="context_requested",
                    session=current_session,
                    last_action=action,
                    requested_context=action.requested_context,
                    applied_artifacts=tuple(applied_artifacts),
                )

            if action.type == AgentActionType.SUMMARIZE:
                current_session = replace(
                    record_action_success(current_session, action),
                    phase=AgentSessionPhase.COMPLETED,
                )
                await self._persist_session(current_session)
                summary = await self._agent_core.summarize_run(current_session)
                return AgentRunOutcome(
                    status="summarized",
                    session=current_session,
                    last_action=action,
                    summary=summary,
                    applied_artifacts=tuple(applied_artifacts),
                )

            if action.type == AgentActionType.REQUEST_APPROVAL:
                return await self._request_approval(current_session, action, applied_artifacts)

            if action.type == AgentActionType.PROPOSE_PATCH:
                await self._ensure_runtime_run_active(current_session)
                current_session, review_outcome = await self._apply_reviewed_patch(
                    current_session,
                    action,
                    applied_artifacts,
                )
                if review_outcome is not None:
                    return review_outcome
                applied_artifacts = list(current_session.prior_artifacts)
                continue

            if action.type == AgentActionType.RUN_COMMAND:
                await self._ensure_runtime_run_active(current_session)
                current_session, command_outcome = await self._run_command(
                    current_session,
                    action,
                    applied_artifacts,
                )
                if command_outcome is not None:
                    return command_outcome
                continue

            if action.type == AgentActionType.COMPLETE:
                await self._ensure_runtime_run_active(current_session)
                run_result = RunResult(
                    run_id=current_session.run_id,
                    status=RunStatus.SUCCEEDED,
                    summary=action.summary_text or action.reason,
                    artifacts=tuple(applied_artifacts),
                )
                try:
                    await self._execution_runtime.finalize_run(str(current_session.run_id), run_result)
                except Exception as exc:
                    failed_session = record_failure(
                        current_session,
                        stage="finalize_run",
                        message=str(exc),
                        action=action,
                    )
                    await self._persist_session(failed_session)
                    return AgentRunOutcome(
                        status="failed",
                        session=failed_session,
                        last_action=action,
                        applied_artifacts=tuple(applied_artifacts),
                    )

                current_session = replace(
                    record_action_success(current_session, action),
                    phase=AgentSessionPhase.COMPLETED,
                )
                await self._persist_session(current_session)
                return AgentRunOutcome(
                    status="completed",
                    session=current_session,
                    last_action=action,
                    applied_artifacts=tuple(applied_artifacts),
                )

            raise AgentStateValidationError(f"Unsupported action type for coordination: {action.type!r}")

    async def resume(self, run_id: str) -> AgentRunOutcome:
        return await self.run(await self._load_session(run_id))

    async def resume_after_context(
        self,
        run_id: str,
        repo_context,
    ) -> AgentRunOutcome:
        session = await self._load_session(run_id)
        action = session.pending_action
        if action is None or action.type != AgentActionType.ASK_CONTEXT:
            raise AgentStateValidationError("resume_after_context requires a pending ask_context action")
        if session.pending_approval_id is not None:
            raise AgentStateValidationError("Cannot attach context while approval is still pending")

        updated_session = record_action_success(session, action, repo_context=repo_context)
        await self._persist_session(updated_session)
        return await self.run(updated_session)

    async def resume_after_approval(
        self,
        run_id: str,
        *,
        approved: bool,
        reviewer: str | None = None,
        comment: str | None = None,
    ) -> AgentRunOutcome:
        session = await self._load_session(run_id)
        action = session.pending_action
        if action is None:
            raise AgentStateValidationError("resume_after_approval requires a pending action")
        if session.pending_approval_id is None:
            raise AgentStateValidationError("resume_after_approval requires a pending approval id")

        record_approval_decision = getattr(self._execution_runtime, "record_approval_decision", None)
        if callable(record_approval_decision):
            await record_approval_decision(
                ApprovalDecision(
                    approval_id=session.pending_approval_id,
                    run_id=session.run_id,
                    approved=approved,
                    reviewer=reviewer,
                    comment=comment,
                )
            )

        if not approved:
            failed_session = record_failure(
                session,
                stage="approval",
                message=comment or "Approval denied",
                action=action,
            )
            failed_session = clear_pending_action(
                failed_session,
                action=action,
                clear_pending_approval=True,
            )
            await self._persist_session(failed_session)
            return AgentRunOutcome(
                status="failed",
                session=failed_session,
                last_action=action,
                approval_id=session.pending_approval_id,
                applied_artifacts=tuple(failed_session.prior_artifacts),
            )

        resume_run = getattr(self._execution_runtime, "resume_run", None)
        if callable(resume_run):
            await resume_run(str(session.run_id))

        if action.type == AgentActionType.REQUEST_APPROVAL:
            updated_session = record_action_success(session, action)
        else:
            updated_session = clear_pending_approval(session)
        await self._persist_session(updated_session)
        return await self.run(updated_session)

    async def _apply_reviewed_patch(
        self,
        session: AgentSession,
        action: AgentAction,
        applied_artifacts: list[ArtifactRef],
    ) -> tuple[AgentSession, AgentRunOutcome | None]:
        patch_intent = self._reset_patch_action(action)
        patch_action = action
        review = None
        retry_count = 0
        seen_failure_codes: set[str] = set()

        while True:
            if patch_action.patch_diff is None or not patch_action.patch_diff.strip():
                try:
                    patch_action = await self._agent_core.generate_patch(session, patch_intent)
                except Exception as exc:
                    failure_code = getattr(exc, "failure_code", None)
                    if self._should_retry_patch_failure(failure_code, retry_count, seen_failure_codes):
                        seen_failure_codes.add(failure_code)
                        retry_count += 1
                        session = record_retryable_failure(
                            session,
                            stage="generate_patch",
                            message=str(exc),
                            code=failure_code,
                        )
                        session = self._replace_tracked_action(session, patch_intent)
                        await self._persist_session(session)
                        patch_action = patch_intent
                        continue

                    failed_session = record_failure(
                        session,
                        stage="generate_patch",
                        message=str(exc),
                        code=failure_code,
                        action=patch_intent,
                    )
                    await self._persist_session(failed_session)
                    return failed_session, AgentRunOutcome(
                        status="failed",
                        session=failed_session,
                        last_action=patch_intent,
                        applied_artifacts=tuple(applied_artifacts),
                    )

                session = self._replace_tracked_action(session, patch_action)
                await self._persist_session(session)

            try:
                review = await self._agent_core.review_patch(session, patch_action)
            except Exception as exc:
                failure_code = getattr(exc, "failure_code", None)
                if self._should_retry_patch_failure(failure_code, retry_count, seen_failure_codes):
                    seen_failure_codes.add(failure_code)
                    retry_count += 1
                    session = record_retryable_failure(
                        session,
                        stage="review_patch",
                        message=str(exc),
                        code=failure_code,
                    )
                    session = self._replace_tracked_action(session, patch_intent)
                    await self._persist_session(session)
                    patch_action = patch_intent
                    continue

                failed_session = record_failure(
                    session,
                    stage="review_patch",
                    message=str(exc),
                    code=failure_code,
                    action=patch_action,
                )
                await self._persist_session(failed_session)
                return failed_session, AgentRunOutcome(
                    status="failed",
                    session=failed_session,
                    last_action=patch_action,
                    applied_artifacts=tuple(applied_artifacts),
                )

            if not review.accepted:
                failed_session = record_failure(
                    session,
                    stage="review_patch",
                    message=review.reason,
                    action=patch_action,
                )
                await self._persist_session(failed_session)
                return failed_session, AgentRunOutcome(
                    status="failed",
                    session=failed_session,
                    last_action=patch_action,
                    patch_review=review,
                    applied_artifacts=tuple(applied_artifacts),
                )

            try:
                self._validate_post_apply_patch(session, patch_action)
            except PostApplyValidationFailure as exc:
                failure_code = getattr(exc, "failure_code", None)
                if self._should_retry_patch_failure(failure_code, retry_count, seen_failure_codes):
                    seen_failure_codes.add(failure_code)
                    retry_count += 1
                    session = record_retryable_failure(
                        session,
                        stage="post_apply_validation",
                        message=str(exc),
                        code=failure_code,
                    )
                    session = self._replace_tracked_action(session, patch_intent)
                    await self._persist_session(session)
                    patch_action = patch_intent
                    continue

                failed_session = record_failure(
                    session,
                    stage="post_apply_validation",
                    message=str(exc),
                    code=failure_code,
                    action=patch_action,
                )
                await self._persist_session(failed_session)
                return failed_session, AgentRunOutcome(
                    status="failed",
                    session=failed_session,
                    last_action=patch_action,
                    patch_review=review,
                    applied_artifacts=tuple(applied_artifacts),
                )

            break

        assert review is not None

        proposal = PatchProposal(
            run_id=session.run_id,
            task_id=patch_action.action_id or new_task_id(),
            summary=patch_action.reason,
            unified_diff=review.patch_diff or patch_action.patch_diff or "",
            target_paths=review.changed_files or patch_action.target_files,
        )

        try:
            artifact = await self._execution_runtime.apply_patch(str(session.run_id), proposal)
        except Exception as exc:
            recovery_outcome = await self._runtime_recovery_outcome(session, patch_action, applied_artifacts)
            if recovery_outcome is not None:
                return recovery_outcome.session, recovery_outcome
            failed_session = record_failure(
                session,
                stage="patch_apply",
                message=str(exc),
                action=patch_action,
            )
            await self._persist_session(failed_session)
            return failed_session, AgentRunOutcome(
                status="failed",
                session=failed_session,
                last_action=patch_action,
                patch_review=review,
                applied_artifacts=tuple(applied_artifacts),
            )

        updated_session = record_action_success(session, patch_action, artifact=artifact)
        updated_session = await self._refresh_repo_context_after_patch(
            updated_session,
            changed_files=review.changed_files or patch_action.target_files,
        )
        updated_session, verification_outcome = await self._run_post_patch_verification(
            updated_session,
            trigger_action=patch_action,
            review=review,
            applied_artifacts=applied_artifacts,
        )
        if verification_outcome is not None:
            return updated_session, verification_outcome
        await self._persist_session(updated_session)
        return updated_session, None

    def _should_retry_patch_failure(
        self,
        failure_code: str | None,
        retry_count: int,
        seen_failure_codes: set[str],
    ) -> bool:
        if failure_code not in RETRYABLE_PATCH_FAILURE_CODES:
            return False
        if retry_count >= MAX_PATCH_RETRIES:
            return False
        if failure_code in seen_failure_codes:
            return False
        return True

    def _reset_patch_action(self, action: AgentAction) -> AgentAction:
        return AgentAction(
            type=action.type,
            reason=action.reason,
            step_id=action.step_id,
            action_id=action.action_id,
            target_files=action.target_files,
            command_argv=action.command_argv,
            cwd=action.cwd,
            allow_file_deletions=False,
            approval_message=action.approval_message,
            approval_risk_reason=action.approval_risk_reason,
            summary_text=action.summary_text,
            requested_context=action.requested_context,
        )

    def _validate_post_apply_patch(
        self,
        session: AgentSession,
        action: AgentAction,
    ) -> None:
        candidates = build_post_apply_candidates(session, patch_diff=action.patch_diff or "")
        run_post_apply_validators(candidates)

    async def _run_command(
        self,
        session: AgentSession,
        action: AgentAction,
        applied_artifacts: list[ArtifactRef],
    ) -> tuple[AgentSession, AgentRunOutcome | None]:
        command_action = action
        if not command_action.command_argv:
            try:
                command_action = await self._agent_core.generate_command(session, action)
            except Exception as exc:
                failed_session = record_failure(
                    session,
                    stage="generate_command",
                    message=str(exc),
                    action=action,
                )
                await self._persist_session(failed_session)
                return failed_session, AgentRunOutcome(
                    status="failed",
                    session=failed_session,
                    last_action=action,
                    applied_artifacts=tuple(applied_artifacts),
                )

            session = self._replace_tracked_action(session, command_action)
            await self._persist_session(session)
            validate_action_for_dispatch(command_action)

        request = CommandRequest(
            run_id=session.run_id,
            task_id=command_action.action_id or new_task_id(),
            argv=command_action.command_argv,
            cwd=command_action.cwd,
        )

        try:
            result = await self._execution_runtime.execute_command(request)
        except Exception as exc:
            recovery_outcome = await self._runtime_recovery_outcome(
                session,
                command_action,
                applied_artifacts,
            )
            if recovery_outcome is not None:
                return recovery_outcome.session, recovery_outcome
            failed_session = record_failure(
                session,
                stage="command",
                message=str(exc),
                action=command_action,
            )
            await self._persist_session(failed_session)
            return failed_session, AgentRunOutcome(
                status="failed",
                session=failed_session,
                last_action=command_action,
                applied_artifacts=tuple(applied_artifacts),
            )

        if result.timed_out or result.cancelled:
            failed_session = record_failure(
                session,
                stage="command",
                message=self._command_failure_message(result),
                details=self._command_failure_details(command_action, result),
                action=command_action,
            )
            await self._persist_session(failed_session)
            return failed_session, AgentRunOutcome(
                status="failed",
                session=failed_session,
                last_action=command_action,
                command_result=result,
                applied_artifacts=tuple(applied_artifacts),
            )

        if result.exit_code not in (None, 0):
            failed_session = record_retryable_action_failure(
                session,
                stage="command",
                message=self._command_failure_message(result),
                code="command_failed",
                action=command_action,
                details=self._command_failure_details(command_action, result),
            )
            failed_session = await self._refresh_repo_context_after_command_failure(
                failed_session,
                action=command_action,
                result=result,
            )
            await self._persist_session(failed_session)
            return failed_session, None

        updated_session = record_action_success(session, command_action)
        updated_session = clear_retryable_failures(updated_session, stage="command")
        await self._persist_session(updated_session)
        return updated_session, None

    async def _request_approval(
        self,
        session: AgentSession,
        action: AgentAction,
        applied_artifacts: list[ArtifactRef],
    ) -> AgentRunOutcome:
        request_approval = getattr(self._execution_runtime, "request_approval", None)
        if callable(request_approval):
            approval_request = ApprovalRequest(
                run_id=session.run_id,
                task_id=action.action_id or new_task_id(),
                reason=action.approval_message or action.reason,
                command_argv=action.command_argv,
            )
            approval_id = await request_approval(str(session.run_id), approval_request)
        else:
            approval_id = None

        updated_session = set_pending_approval(session, action=action, approval_id=approval_id)
        await self._persist_session(updated_session)
        return AgentRunOutcome(
            status="approval_requested",
            session=updated_session,
            last_action=action,
            approval_id=approval_id,
            applied_artifacts=tuple(applied_artifacts),
        )

    async def _select_action(self, session: AgentSession) -> tuple[AgentSession, AgentAction]:
        if session.pending_action is not None:
            action = ensure_action_id(session, session.pending_action)
            if action is not session.pending_action:
                session = replace(session, pending_action=action)
            return session, action

        action = await self._agent_core.next_action(session)
        recorded_session, identified_action = record_selected_action(session, action)
        await self._persist_session(recorded_session)
        return recorded_session, identified_action

    def _command_failure_message(self, result: CommandResult) -> str:
        if result.timed_out:
            return "Command timed out"
        if result.cancelled:
            return "Command was cancelled"
        return f"Command failed with exit code {result.exit_code}"

    async def _run_post_patch_verification(
        self,
        session: AgentSession,
        *,
        trigger_action: AgentAction,
        review,
        applied_artifacts: list[ArtifactRef],
    ) -> tuple[AgentSession, AgentRunOutcome | None]:
        workspace_root = await self._workspace_root_for_session(session)
        planned_verifications = self._agent_core.plan_patch_verification(
            session,
            changed_files=self._changed_files_for_patch(review, trigger_action),
            deleted_files=self._deleted_files_for_patch(review, trigger_action),
            workspace_root=workspace_root,
        )
        if not planned_verifications:
            return session, None

        updated_session = session
        for planned_verification in planned_verifications:
            updated_session, verification_outcome = await self._execute_planned_verification(
                updated_session,
                trigger_action=trigger_action,
                planned_verification=planned_verification,
                applied_artifacts=applied_artifacts,
            )
            if verification_outcome is not None:
                return updated_session, verification_outcome
        await self._persist_session(updated_session)
        return updated_session, None

    async def _execute_planned_verification(
        self,
        session: AgentSession,
        *,
        trigger_action: AgentAction,
        planned_verification: AgentVerification,
        applied_artifacts: list[ArtifactRef],
    ) -> tuple[AgentSession, AgentRunOutcome | None]:
        if not planned_verification.command_argv:
            updated_session = self._append_verification_result(
                session,
                replace(
                    planned_verification,
                    trigger_action_id=trigger_action.action_id,
                ),
            )
            return updated_session, None

        if planned_verification.kind == "fallback_pytest":
            workspace_root = await self._workspace_root_for_session(session)
            fallback_error = self._validate_fallback_test_command(
                planned_verification.command_argv,
                workspace_root=workspace_root,
            )
            if fallback_error is not None:
                updated_session = self._append_verification_result(
                    session,
                    replace(
                        planned_verification,
                        kind="fallback_pytest_rejected",
                        verification_level="functional_verification_missing",
                        stderr=fallback_error,
                        trigger_action_id=trigger_action.action_id,
                    ),
                )
                updated_session = self._append_warning(
                    updated_session,
                    f"Fallback test command rejected: {fallback_error}",
                )
                await self._persist_session(updated_session)
                return updated_session, None

        verification_action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run automatic post-patch verification",
            target_files=planned_verification.changed_files,
            command_argv=planned_verification.command_argv,
        )
        request = CommandRequest(
            run_id=session.run_id,
            task_id=new_task_id(),
            argv=planned_verification.command_argv,
        )

        try:
            result = await self._execution_runtime.execute_command(request)
        except Exception as exc:
            recovery_outcome = await self._runtime_recovery_outcome(
                session,
                verification_action,
                applied_artifacts,
            )
            if recovery_outcome is not None:
                return recovery_outcome.session, recovery_outcome
            failed_session = self._append_verification_result(
                session,
                replace(
                    planned_verification,
                    stdout="",
                    stderr=str(exc),
                    failure_signature=self._verification_failure_signature(
                        planned_verification.command_argv,
                        CommandResult(
                            run_id=session.run_id,
                            task_id=request.task_id,
                            exit_code=None,
                            stderr=str(exc),
                        ),
                    ),
                    trigger_action_id=trigger_action.action_id,
                ),
            )
            failed_session = record_failure(
                failed_session,
                stage="verification",
                message=str(exc),
                action=None,
            )
            await self._persist_session(failed_session)
            return failed_session, AgentRunOutcome(
                status="failed",
                session=failed_session,
                last_action=trigger_action,
                applied_artifacts=tuple(applied_artifacts),
            )

        verification_level = planned_verification.verification_level
        if verification_level is None and planned_verification.kind == "py_compile":
            verification_level = "syntax_only"
        if verification_level is None and planned_verification.kind == "targeted_pytest":
            verification_level = "targeted_tests_passed"
        if verification_level is None and planned_verification.kind == "fallback_pytest":
            verification_level = "fallback_tests_passed"
        if result.exit_code not in (None, 0) and planned_verification.kind == "targeted_pytest":
            verification_level = "targeted_tests_failed"
        if result.exit_code not in (None, 0) and planned_verification.kind == "fallback_pytest":
            verification_level = "fallback_tests_failed"
        verification_result = replace(
            planned_verification,
            verification_level=verification_level,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            failure_signature=self._verification_failure_signature(
                planned_verification.command_argv,
                result,
            ) if result.exit_code not in (None, 0) else None,
            trigger_action_id=trigger_action.action_id,
        )
        updated_session = self._append_verification_result(session, verification_result)

        if result.timed_out or result.cancelled:
            failed_session = record_failure(
                updated_session,
                stage="verification",
                message=self._command_failure_message(result),
                details=self._command_failure_details(verification_action, result),
                action=None,
            )
            await self._persist_session(failed_session)
            return failed_session, AgentRunOutcome(
                status="failed",
                session=failed_session,
                last_action=trigger_action,
                command_result=result,
                applied_artifacts=tuple(applied_artifacts),
            )

        if result.exit_code not in (None, 0):
            failed_session = record_retryable_action_failure(
                updated_session,
                stage="command",
                message=self._command_failure_message(result),
                code="command_failed",
                action=verification_action,
                details={
                    **self._command_failure_details(verification_action, result),
                    "failure_signature": verification_result.failure_signature or "",
                    "verification_level": verification_result.verification_level or "",
                },
            )
            failed_session = await self._refresh_repo_context_after_command_failure(
                failed_session,
                action=verification_action,
                result=result,
            )
            await self._persist_session(failed_session)
            return failed_session, None

        updated_session = clear_retryable_failures(updated_session, stage="command")
        return updated_session, None

    async def _persist_session(self, session: AgentSession) -> None:
        if self._session_store is None:
            return
        await self._session_store.save_agent_session(session)

    async def _workspace_root_for_session(self, session: AgentSession) -> str | None:
        if self._repo_store is None:
            return None
        workspace = await self._repo_store.get_workspace(session.workspace_id)
        if workspace is None:
            return None
        return workspace.root_path

    async def _refresh_repo_context_after_patch(
        self,
        session: AgentSession,
        *,
        changed_files: tuple[str, ...],
    ) -> AgentSession:
        if self._repo_intelligence is None or self._repo_store is None or not changed_files:
            return session

        workspace = await self._repo_store.get_workspace(session.workspace_id)
        if workspace is None:
            return self._append_warning(
                session,
                f"repo_intelligence refresh skipped: workspace {session.workspace_id} is unavailable",
            )

        try:
            await self._repo_intelligence.refresh_index(workspace, changed_files)
            impact = await self._repo_intelligence.analyze_impact(workspace, changed_files)
            refreshed_context = await self._repo_intelligence.build_context(
                RepoContextRequest(
                    workspace_id=session.workspace_id,
                    run_id=session.run_id,
                    prompt=session.user_request,
                    target_paths=impact.impacted_paths or changed_files,
                )
            )
        except Exception as exc:
            return self._append_warning(
                session,
                f"repo_intelligence refresh after patch failed: {exc}",
            )

        merged_warnings = self._merge_warnings(
            session.warnings,
            impact.warnings,
            refreshed_context.warnings,
        )
        refreshed_context = replace(
            refreshed_context,
            warnings=tuple(self._merge_warnings(impact.warnings, refreshed_context.warnings)),
        )
        return replace(
            session,
            repo_context=refreshed_context,
            warnings=merged_warnings,
        )

    async def _refresh_repo_context_after_command_failure(
        self,
        session: AgentSession,
        *,
        action: AgentAction,
        result: CommandResult,
    ) -> AgentSession:
        if self._repo_intelligence is None or self._repo_store is None:
            return session

        workspace = await self._repo_store.get_workspace(session.workspace_id)
        if workspace is None:
            return self._append_warning(
                session,
                f"repo_intelligence command-failure refresh skipped: workspace {session.workspace_id} is unavailable",
            )

        target_paths = tuple(action.target_files) or self._repo_context_paths(session)
        warnings: list[str] = []
        symbol_matches = ()
        try:
            symbol_matches = await self._search_failure_symbols(workspace, session, action, result)
        except Exception as exc:
            warnings.append(f"repo_intelligence symbol search after command failure failed: {exc}")

        try:
            impact = await self._repo_intelligence.analyze_impact(workspace, target_paths)
        except Exception as exc:
            warnings.append(f"repo_intelligence impact analysis after command failure failed: {exc}")
            impact = None

        context_target_paths = self._merge_target_paths(
            target_paths,
            tuple(match.path for match in symbol_matches),
            impact.impacted_paths if impact is not None else (),
        )
        if not context_target_paths:
            context_target_paths = target_paths

        try:
            refreshed_context = await self._repo_intelligence.build_context(
                RepoContextRequest(
                    workspace_id=session.workspace_id,
                    run_id=session.run_id,
                    prompt=self._command_failure_prompt(session, action, result),
                    target_paths=context_target_paths,
                )
            )
        except Exception as exc:
            return self._append_warning(
                session,
                f"repo_intelligence build_context after command failure failed: {exc}",
            )

        merged_symbols = tuple(self._merge_symbol_matches(
            session.repo_context.symbols if session.repo_context is not None else (),
            symbol_matches,
            refreshed_context.symbols,
        ))
        merged_warning_values = self._merge_warnings(
            session.warnings,
            warnings,
            impact.warnings if impact is not None else (),
            refreshed_context.warnings,
        )
        refreshed_context = replace(
            refreshed_context,
            symbols=merged_symbols,
            warnings=tuple(self._merge_warnings(
                warnings,
                impact.warnings if impact is not None else (),
                refreshed_context.warnings,
            )),
        )
        return replace(
            session,
            repo_context=refreshed_context,
            warnings=merged_warning_values,
        )

    async def _ensure_runtime_run_active(self, session: AgentSession) -> None:
        claim_run = getattr(self._execution_runtime, "claim_run", None)
        if not callable(claim_run):
            return
        claimed = await claim_run(
            str(session.run_id),
            self._CLAIM_WORKER_ID,
            self._CLAIM_LEASE_SECONDS,
        )
        if claimed is None:
            raise AgentStateValidationError(
                f"Run {session.run_id} could not be claimed before executing side effects"
            )

    async def _load_session(self, run_id: str) -> AgentSession:
        if self._session_store is None:
            raise AgentStateValidationError("Coordinator resume requires a session_store")
        session = await self._session_store.load_agent_session(run_id)
        if session is None:
            raise AgentStateValidationError(f"No persisted AgentSession found for run {run_id}")
        return session

    def _replace_tracked_action(self, session: AgentSession, action: AgentAction) -> AgentSession:
        updated_history = list(session.action_history)
        if action.action_id is not None:
            for index, existing in enumerate(updated_history):
                if existing.action_id == action.action_id:
                    updated_history[index] = action
                    break
        pending_action = session.pending_action
        if pending_action is not None and pending_action.action_id == action.action_id:
            pending_action = action
        return replace(session, action_history=updated_history, pending_action=pending_action)

    async def _runtime_recovery_outcome(
        self,
        session: AgentSession,
        action: AgentAction,
        applied_artifacts: list[ArtifactRef],
    ) -> AgentRunOutcome | None:
        get_recovery_status = getattr(self._execution_runtime, "get_recovery_status", None)
        if not callable(get_recovery_status):
            return None

        recovery_status = await get_recovery_status(str(session.run_id))
        if not isinstance(recovery_status, RecoveryStatus):
            return None

        recovery_session = replace(session, phase=AgentSessionPhase.NEEDS_RECOVERY)
        await self._persist_session(recovery_session)
        return AgentRunOutcome(
            status="needs_recovery",
            session=recovery_session,
            last_action=action,
            recovery_status=recovery_status,
            applied_artifacts=tuple(applied_artifacts),
        )

    def _append_warning(self, session: AgentSession, warning: str) -> AgentSession:
        return replace(
            session,
            warnings=self._merge_warnings(session.warnings, (warning,)),
        )

    def _merge_warnings(self, *warning_groups: tuple[str, ...] | list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for warning_group in warning_groups:
            for warning in warning_group:
                if not warning or warning in seen:
                    continue
                seen.add(warning)
                merged.append(warning)
        return merged

    def _command_failure_details(
        self,
        action: AgentAction,
        result: CommandResult,
    ) -> dict[str, str]:
        details = {
            "exit_code": "" if result.exit_code is None else str(result.exit_code),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "command_argv": " ".join(action.command_argv),
        }
        if action.cwd is not None:
            details["cwd"] = action.cwd
        if result.termination_reason is not None:
            details["termination_reason"] = result.termination_reason
        return {key: value for key, value in details.items() if value}

    def _append_verification_result(
        self,
        session: AgentSession,
        verification: AgentVerification,
    ) -> AgentSession:
        return replace(
            session,
            verification_history=[*session.verification_history, verification],
        )

    def _verification_failure_signature(
        self,
        command_argv: tuple[str, ...],
        result: CommandResult,
    ) -> str:
        stderr = result.stderr.strip().splitlines()
        stdout = result.stdout.strip().splitlines()
        signature_input = "\n".join(
            part
            for part in (
                " ".join(command_argv),
                "" if result.exit_code is None else str(result.exit_code),
                stderr[0] if stderr else "",
                stdout[0] if stdout else "",
            )
            if part
        )
        digest = hashlib.sha1(signature_input.encode("utf-8")).hexdigest()[:12]
        prefix = "verification"
        if command_argv[0:3] == ("python", "-m", "py_compile"):
            prefix = "py_compile"
        elif len(command_argv) >= 3 and command_argv[1:3] == ("-m", "pytest"):
            prefix = "pytest"
        return f"{prefix}:{digest}"

    def _changed_files_for_patch(
        self,
        review,
        action: AgentAction,
    ) -> tuple[str, ...]:
        extracted = self._patch_change_entries(review, action)
        if extracted:
            return tuple(path for path, _deleted in extracted)
        return review.changed_files or action.target_files

    def _deleted_files_for_patch(
        self,
        review,
        action: AgentAction,
    ) -> tuple[str, ...]:
        return tuple(path for path, deleted in self._patch_change_entries(review, action) if deleted)

    def _patch_change_entries(
        self,
        review,
        action: AgentAction,
    ) -> tuple[tuple[str, bool], ...]:
        patch_diff = review.patch_diff or action.patch_diff or ""
        if not patch_diff.strip():
            return ()
        try:
            return extract_patch_changed_files(patch_diff)
        except AgentStateValidationError:
            return ()

    def _validate_fallback_test_command(
        self,
        command_argv: tuple[str, ...],
        *,
        workspace_root: str | None,
    ) -> str | None:
        if not command_argv:
            return "fallback_test_command must not be empty"

        rejected_fragments = ("&&", "||", ";", "|", "`", "$(", ">", "<")
        for token in command_argv:
            if any(fragment in token for fragment in rejected_fragments):
                return "fallback_test_command contains a disallowed shell fragment"

        if command_argv[0] not in {"python", "./.venv/bin/python", ".venv/bin/python"}:
            return "fallback_test_command must start with python or ./.venv/bin/python"
        if len(command_argv) < 4 or command_argv[1:3] != ("-m", "pytest"):
            return "fallback_test_command must use the form 'python -m pytest <path>'"

        target_path = command_argv[3].strip()
        if not target_path or target_path in {".", "./"} or target_path.startswith("-"):
            return "fallback_test_command must include a non-empty pytest target path"

        if len(command_argv) > 4:
            if len(command_argv) != 6 or command_argv[4] != "-k":
                return "fallback_test_command only allows an optional '-k <expression>' suffix"
            expression = command_argv[5].strip()
            if not expression or not re.fullmatch(r"[A-Za-z0-9_ .-]+", expression):
                return "fallback_test_command '-k' expression contains unsupported characters"

        target_candidate = Path(target_path)
        if target_candidate.is_absolute():
            return "fallback_test_command pytest target must be workspace-relative"
        if workspace_root is not None:
            root = Path(workspace_root).resolve(strict=False)
            resolved = (root / target_candidate).resolve(strict=False)
            if not resolved.is_relative_to(root):
                return "fallback_test_command pytest target must stay inside the workspace"
        return None

    async def _search_failure_symbols(
        self,
        workspace,
        session: AgentSession,
        action: AgentAction,
        result: CommandResult,
    ):
        if self._repo_intelligence is None:
            return ()

        matches = []
        seen = set()
        for query in self._command_failure_queries(session, action, result):
            query_matches = await self._repo_intelligence.search_symbols(workspace, query)
            for match in query_matches:
                key = (match.name, match.kind, match.path, match.line)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(match)
            if len(matches) >= 12:
                break
        return tuple(matches[:12])

    def _command_failure_queries(
        self,
        session: AgentSession,
        action: AgentAction,
        result: CommandResult,
    ) -> tuple[str, ...]:
        candidates: list[str] = []
        candidate_text = "\n".join(
            part
            for part in (
                result.stderr,
                result.stdout,
                session.user_request,
                " ".join(action.target_files),
            )
            if part
        )
        for token in _FAILURE_QUERY_PATTERN.findall(candidate_text):
            lowered = token.lower()
            if lowered in _FAILURE_QUERY_STOPWORDS:
                continue
            if token not in candidates:
                candidates.append(token)
            if len(candidates) >= 5:
                break

        if not candidates:
            for path in action.target_files:
                stem = Path(path).stem
                if stem and stem.lower() not in _FAILURE_QUERY_STOPWORDS:
                    candidates.append(stem)
                    break
        return tuple(candidates)

    def _command_failure_prompt(
        self,
        session: AgentSession,
        action: AgentAction,
        result: CommandResult,
    ) -> str:
        message = self._command_failure_message(result)
        details = self._command_failure_details(action, result)
        serialized_details = "\n".join(f"{key}: {value}" for key, value in details.items())
        return (
            f"{session.user_request}\n\n"
            f"Verification command failed.\n"
            f"{message}\n"
            f"{serialized_details}"
        )

    def _repo_context_paths(self, session: AgentSession) -> tuple[str, ...]:
        if session.repo_context is None:
            return ()
        return tuple(item.path for item in session.repo_context.file_summaries if item.path)

    def _merge_target_paths(self, *groups: tuple[str, ...]) -> tuple[str, ...]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for path in group:
                if not path or path in seen:
                    continue
                seen.add(path)
                merged.append(path)
        return tuple(merged)

    def _merge_symbol_matches(self, *groups) -> list:
        merged = []
        seen = set()
        for group in groups:
            for match in group:
                key = (match.name, match.kind, match.path, match.line)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(match)
        return merged
