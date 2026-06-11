from __future__ import annotations

from dataclasses import replace

from packages.shared_types import (
    ApprovalDecision,
    ApprovalRequest,
    ArtifactRef,
    CommandRequest,
    CommandResult,
    PatchProposal,
    RecoveryStatus,
    RunResult,
    RunStatus,
    TaskStatus,
    new_task_id,
)
from services.execution_runtime.service import ExecutionRuntimeService

from .models import AgentAction, AgentActionType, AgentRunOutcome, AgentSession, AgentSessionPhase
from .reducer import (
    clear_pending_action,
    clear_pending_approval,
    ensure_action_id,
    has_completed_action,
    record_action_success,
    record_failure,
    record_selected_action,
    set_pending_approval,
)
from .session_store import AgentSessionStore
from .service import AgentCoreService
from .validation import AgentStateValidationError, validate_action_for_dispatch


class AgentCoreCoordinator:
    """Thin orchestration layer that dispatches validated agent actions to the runtime."""

    def __init__(
        self,
        *,
        agent_core: AgentCoreService,
        execution_runtime: ExecutionRuntimeService,
        session_store: AgentSessionStore | None = None,
    ) -> None:
        self._agent_core = agent_core
        self._execution_runtime = execution_runtime
        self._session_store = session_store

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
                current_session, command_outcome = await self._run_command(
                    current_session,
                    action,
                    applied_artifacts,
                )
                if command_outcome is not None:
                    return command_outcome
                continue

            if action.type == AgentActionType.COMPLETE:
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
        try:
            review = await self._agent_core.review_patch(session, action)
        except Exception as exc:
            failed_session = record_failure(session, stage="review_patch", message=str(exc), action=action)
            await self._persist_session(failed_session)
            return failed_session, AgentRunOutcome(
                status="failed",
                session=failed_session,
                last_action=action,
                applied_artifacts=tuple(applied_artifacts),
            )

        if not review.accepted:
            failed_session = record_failure(
                session,
                stage="review_patch",
                message=review.reason,
                action=action,
            )
            await self._persist_session(failed_session)
            return failed_session, AgentRunOutcome(
                status="failed",
                session=failed_session,
                last_action=action,
                patch_review=review,
                applied_artifacts=tuple(applied_artifacts),
            )

        proposal = PatchProposal(
            run_id=session.run_id,
            task_id=action.action_id or new_task_id(),
            summary=action.reason,
            unified_diff=review.patch_diff or action.patch_diff or "",
            target_paths=review.changed_files or action.target_files,
        )

        try:
            artifact = await self._execution_runtime.apply_patch(str(session.run_id), proposal)
        except Exception as exc:
            recovery_outcome = await self._runtime_recovery_outcome(
                session,
                action,
                applied_artifacts,
            )
            if recovery_outcome is not None:
                return recovery_outcome.session, recovery_outcome
            failed_session = record_failure(session, stage="patch_apply", message=str(exc), action=action)
            await self._persist_session(failed_session)
            return failed_session, AgentRunOutcome(
                status="failed",
                session=failed_session,
                last_action=action,
                patch_review=review,
                applied_artifacts=tuple(applied_artifacts),
            )

        updated_session = record_action_success(session, action, artifact=artifact)
        await self._persist_session(updated_session)
        return updated_session, None

    async def _run_command(
        self,
        session: AgentSession,
        action: AgentAction,
        applied_artifacts: list[ArtifactRef],
    ) -> tuple[AgentSession, AgentRunOutcome | None]:
        request = CommandRequest(
            run_id=session.run_id,
            task_id=action.action_id or new_task_id(),
            argv=action.command_argv,
            cwd=action.cwd,
        )

        try:
            result = await self._execution_runtime.execute_command(request)
        except Exception as exc:
            recovery_outcome = await self._runtime_recovery_outcome(
                session,
                action,
                applied_artifacts,
            )
            if recovery_outcome is not None:
                return recovery_outcome.session, recovery_outcome
            failed_session = record_failure(session, stage="command", message=str(exc), action=action)
            await self._persist_session(failed_session)
            return failed_session, AgentRunOutcome(
                status="failed",
                session=failed_session,
                last_action=action,
                applied_artifacts=tuple(applied_artifacts),
            )

        if result.exit_code not in (None, 0) or result.timed_out or result.cancelled:
            failed_session = record_failure(
                session,
                stage="command",
                message=self._command_failure_message(result),
                action=action,
            )
            await self._persist_session(failed_session)
            return failed_session, AgentRunOutcome(
                status="failed",
                session=failed_session,
                last_action=action,
                command_result=result,
                applied_artifacts=tuple(applied_artifacts),
            )

        updated_session = record_action_success(session, action)
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

    async def _persist_session(self, session: AgentSession) -> None:
        if self._session_store is None:
            return
        await self._session_store.save_agent_session(session)

    async def _load_session(self, run_id: str) -> AgentSession:
        if self._session_store is None:
            raise AgentStateValidationError("Coordinator resume requires a session_store")
        session = await self._session_store.load_agent_session(run_id)
        if session is None:
            raise AgentStateValidationError(f"No persisted AgentSession found for run {run_id}")
        return session

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
