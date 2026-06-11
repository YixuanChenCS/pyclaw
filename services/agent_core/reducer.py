from __future__ import annotations

from dataclasses import replace

from packages.shared_types import ArtifactRef, RepoContextResult, TaskStatus

from .models import AgentAction, AgentActionType, AgentFailure, AgentSession, AgentSessionPhase
from .validation import AgentStateValidationError


def ensure_action_id(session: AgentSession, action: AgentAction) -> AgentAction:
    if action.action_id is not None:
        if not action.action_id.strip():
            raise AgentStateValidationError("AgentAction.action_id must be non-empty when provided")
        return action

    step_scope = action.step_id or action.type.value
    return replace(
        action,
        action_id=f"action_{session.iteration_count + 1}_{action.type.value}_{step_scope}",
    )


def record_selected_action(session: AgentSession, action: AgentAction) -> tuple[AgentSession, AgentAction]:
    identified = ensure_action_id(session, action)
    return (
        replace(
            session,
            action_history=[*session.action_history, identified],
            pending_action=identified,
            phase=_phase_for_selected_action(identified),
            iteration_count=session.iteration_count + 1,
        ),
        identified,
    )


def record_action_success(
    session: AgentSession,
    action: AgentAction,
    *,
    status: TaskStatus = TaskStatus.SUCCEEDED,
    artifact: ArtifactRef | None = None,
    repo_context: RepoContextResult | None = None,
    clear_pending: bool = True,
    clear_pending_approval: bool = True,
) -> AgentSession:
    identified = ensure_action_id(session, action)
    updated = _update_step_status(session, identified, status)

    completed_action_ids = list(updated.completed_action_ids)
    if identified.action_id is not None and identified.action_id not in completed_action_ids:
        completed_action_ids.append(identified.action_id)

    prior_artifacts = list(updated.prior_artifacts)
    if artifact is not None:
        prior_artifacts.append(artifact)

    pending_action = updated.pending_action
    if clear_pending and _pending_matches(updated, identified):
        pending_action = None

    pending_approval_id = updated.pending_approval_id
    if clear_pending_approval:
        pending_approval_id = None

    return replace(
        updated,
        phase=AgentSessionPhase.READY,
        repo_context=repo_context or updated.repo_context,
        prior_artifacts=prior_artifacts,
        pending_action=pending_action,
        pending_approval_id=pending_approval_id,
        completed_action_ids=completed_action_ids,
    )


def record_failure(
    session: AgentSession,
    *,
    stage: str,
    message: str,
    action: AgentAction | None = None,
) -> AgentSession:
    identified = ensure_action_id(session, action) if action is not None else None
    failed = replace(
        session,
        failure_history=[*session.failure_history, AgentFailure(stage=stage, message=message)],
    )
    if identified is None:
        return failed

    updated = _update_step_status(failed, identified, TaskStatus.FAILED)
    pending_action = updated.pending_action
    if _pending_matches(updated, identified):
        pending_action = None
    return replace(
        updated,
        phase=AgentSessionPhase.FAILED,
        pending_action=pending_action,
        pending_approval_id=None,
    )


def set_pending_approval(
    session: AgentSession,
    *,
    action: AgentAction,
    approval_id: str | None,
) -> AgentSession:
    identified = ensure_action_id(session, action)
    pending_action = identified if _pending_matches(session, identified) or session.pending_action is None else session.pending_action
    return replace(
        session,
        pending_action=pending_action,
        pending_approval_id=approval_id,
        phase=AgentSessionPhase.AWAITING_APPROVAL,
    )


def clear_pending_action(
    session: AgentSession,
    *,
    action: AgentAction | None = None,
    clear_pending_approval: bool = False,
) -> AgentSession:
    if action is not None and not _pending_matches(session, action):
        return session
    return replace(
        session,
        pending_action=None,
        phase=AgentSessionPhase.READY,
        pending_approval_id=None if clear_pending_approval else session.pending_approval_id,
    )


def clear_pending_approval(session: AgentSession) -> AgentSession:
    return replace(session, phase=AgentSessionPhase.READY, pending_approval_id=None)


def has_completed_action(session: AgentSession, action: AgentAction | None) -> bool:
    if action is None or action.action_id is None:
        return False
    return action.action_id in session.completed_action_ids


def _update_step_status(
    session: AgentSession,
    action: AgentAction,
    status: TaskStatus,
) -> AgentSession:
    plan = session.current_plan
    if plan is None:
        return session

    step_index = _find_plan_step_index(plan.steps, action)
    if step_index is None:
        return session

    current_step = plan.steps[step_index]
    if current_step.status == status:
        return session

    updated_steps = list(plan.steps)
    updated_steps[step_index] = replace(current_step, status=status)
    return replace(session, current_plan=replace(plan, steps=updated_steps))


def _find_plan_step_index(steps, action: AgentAction) -> int | None:
    if action.step_id is not None:
        for index, step in enumerate(steps):
            if step.step_id == action.step_id:
                return index
        raise AgentStateValidationError(
            f"AgentAction.step_id {action.step_id!r} does not match any current plan step"
        )

    expected_kind = {
        "ask_context": "inspect",
        "run_command": "command",
        "propose_patch": "patch",
        "request_approval": "approval",
        "summarize": "summarize",
        "complete": "complete",
    }.get(action.type.value)
    if expected_kind is None:
        return None

    for index, step in enumerate(steps):
        if step.status == TaskStatus.PENDING and step.kind.strip().lower() == expected_kind:
            return index

    for index, step in enumerate(steps):
        if step.status == TaskStatus.PENDING:
            return index

    return None


def _pending_matches(session: AgentSession, action: AgentAction) -> bool:
    pending = session.pending_action
    if pending is None:
        return False
    if pending.action_id is not None and action.action_id is not None:
        return pending.action_id == action.action_id
    return pending == action


def _phase_for_selected_action(action: AgentAction) -> AgentSessionPhase:
    if action.type == AgentActionType.ASK_CONTEXT:
        return AgentSessionPhase.AWAITING_CONTEXT
    return AgentSessionPhase.EXECUTING
