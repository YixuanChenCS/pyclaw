from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from packages.shared_types import (
    ArtifactRef,
    CommandResult,
    EventType,
    PatchProposal,
    RecoveryOption,
    RecoveryState,
    RecoveryStatus,
    RepoContextResult,
    RunRequest,
    Session,
    RunStatus,
    TaskStatus,
    Workspace,
    new_run_id,
    new_workspace_id,
)
from services.agent_core import (
    AgentAction,
    AgentActionType,
    AgentCoreCoordinator,
    AgentPlan,
    AgentSession,
    AgentSessionPhase,
    AgentStep,
    LocalAgentCoreService,
    PatchReview,
    RunSummary,
)
from services.agent_core.validation import MAX_AGENT_ITERATIONS
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class _SequencedFakeAgentCore:
    def __init__(
        self,
        *,
        actions: list[AgentAction],
        review: PatchReview | Exception | None = None,
        log: list[str] | None = None,
    ) -> None:
        self._actions = list(actions)
        self._review = review or PatchReview(accepted=True, reason="ok", changed_files=("app.py",))
        self._log = log if log is not None else []

    @property
    def model_client(self):
        return None

    def create_session(self, **kwargs):
        return AgentSession(**kwargs)

    async def create_plan(self, session):
        raise NotImplementedError

    async def next_action(self, session):
        if not self._actions:
            raise AssertionError("No more fake actions available")
        action = self._actions.pop(0)
        self._log.append(f"next_action:{action.type.value}")
        return action

    async def review_patch(self, session, proposed_action):
        self._log.append("review_patch")
        if isinstance(self._review, Exception):
            raise self._review
        return self._review

    async def summarize_run(self, session):
        self._log.append("summarize_run")
        return RunSummary(final_status="completed")


class _RecordingFakeRuntime:
    def __init__(self, *, log: list[str] | None = None) -> None:
        self.calls: list[str] = []
        self.finalized_status: RunStatus | None = None
        self.finalized_summary: str | None = None
        self._log = log if log is not None else []
        self.recorded_decisions: list[tuple[bool, str | None, str | None]] = []
        self.command_requests = []
        self.patch_proposals = []
        self.approval_requests = []

    async def enqueue_run(self, request):
        raise NotImplementedError

    async def cancel_run(self, run_id, reason=None):
        raise NotImplementedError

    async def stream_events(self, run_id):
        raise NotImplementedError

    async def execute_command(self, request):
        self.calls.append("execute_command")
        self._log.append("execute_command")
        self.command_requests.append(request)
        return CommandResult(run_id=request.run_id, task_id=request.task_id, exit_code=0)

    async def apply_patch(self, run_id, proposal):
        self.calls.append("apply_patch")
        self._log.append("apply_patch")
        self.patch_proposals.append(proposal)
        return ArtifactRef(
            artifact_id="artifact_patch",
            run_id=proposal.run_id,
            artifact_type="patch",
            task_id=proposal.task_id,
            label="patch",
            uri="memory://patch",
        )

    async def request_approval(self, run_id, request):
        self.calls.append("request_approval")
        self._log.append("request_approval")
        self.approval_requests.append(request)
        return "approval_123"

    async def record_approval_decision(self, decision):
        self.calls.append("record_approval_decision")
        self._log.append("record_approval_decision")
        self.recorded_decisions.append((decision.approved, decision.reviewer, decision.comment))

    async def resume_run(self, run_id):
        self.calls.append("resume_run")
        self._log.append("resume_run")

    async def attach_artifacts(self, run_id, artifacts):
        self.calls.append("attach_artifacts")

    async def finalize_run(self, run_id, result):
        self.calls.append("finalize_run")
        self._log.append("finalize_run")
        self.finalized_status = result.status
        self.finalized_summary = result.summary

    async def get_recovery_status(self, run_id):
        return None

    async def rollback_task(self, run_id, task_id):
        raise NotImplementedError

    async def deploy(self, request):
        raise NotImplementedError


class _FailingRuntime(_RecordingFakeRuntime):
    async def execute_command(self, request):
        self.calls.append("execute_command")
        self._log.append("execute_command")
        raise RuntimeError("runtime command exploded")


class _RecoveryRuntime(_RecordingFakeRuntime):
    def __init__(self, *, recovery_status: RecoveryStatus, log: list[str] | None = None) -> None:
        super().__init__(log=log)
        self._recovery_status = recovery_status

    async def execute_command(self, request):
        self.calls.append("execute_command")
        self._log.append("execute_command")
        raise InvalidRunStateError("task started and cannot be replayed safely")

    async def get_recovery_status(self, run_id):
        return self._recovery_status


class _NoRuntimeCalls(_RecordingFakeRuntime):
    async def execute_command(self, request):
        raise AssertionError("execute_command should not be called")

    async def apply_patch(self, run_id, proposal):
        raise AssertionError("apply_patch should not be called")

    async def finalize_run(self, run_id, result):
        raise AssertionError("finalize_run should not be called")

    async def request_approval(self, run_id, request):
        raise AssertionError("request_approval should not be called")

    async def record_approval_decision(self, decision):
        raise AssertionError("record_approval_decision should not be called")

    async def resume_run(self, run_id):
        raise AssertionError("resume_run should not be called")


class _RecordingSessionStore:
    def __init__(self, initial_session: AgentSession | None = None, *, log: list[str] | None = None) -> None:
        self.saved_sessions: list[AgentSession] = []
        self._loaded_session = initial_session
        self._log = log if log is not None else []

    async def save_agent_session(self, session):
        self._log.append("save_agent_session")
        self.saved_sessions.append(session)
        self._loaded_session = session

    async def load_agent_session(self, run_id):
        return self._loaded_session


class _InMemoryRepoStore:
    def __init__(self, workspaces: dict[str, Workspace]) -> None:
        self._workspaces = dict(workspaces)

    async def get_workspace(self, workspace_id):
        return self._workspaces.get(str(workspace_id))


class TestAgentCoreCoordinator(unittest.IsolatedAsyncioTestCase):
    def _make_session(self):
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        return AgentSession(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Run the coordinator deterministically",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
            ),
        )

    def _make_session_with_plan(self, *, steps, **overrides):
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        service = LocalAgentCoreService()
        return service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Run the coordinator deterministically",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
            ),
            current_plan=AgentPlan(
                goal="Execute the current plan",
                steps=list(steps),
            ),
            **overrides,
        )

    def _make_runtime(self, root: Path, workspace: Workspace):
        repository = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
        runtime = LocalExecutionRuntimeService(
            repository=repository,
            repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
            stream_poll_interval=0.01,
        )
        return runtime, repository

    def _make_run_request(self, workspace: Workspace) -> RunRequest:
        session = Session(workspace_id=workspace.workspace_id, title="agent-core-runner")
        return RunRequest(
            workspace_id=workspace.workspace_id,
            session_id=session.session_id,
            prompt="Run the coordinator deterministically",
        )

    async def test_coordinator_dispatches_patch_command_complete_in_exact_order(self):
        # Verifies end-to-end action dispatch order through review, runtime patch apply, command execution, and finalization.
        # This catches applying patches before review, skipping execute_command dispatch, or forgetting to finalize completion.
        # The expected order is correct because the fake core emits patch -> command -> complete and the coordinator must honor that sequence.
        log: list[str] = []
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch app.py",
                    target_files=("app.py",),
                    patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
                ),
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run targeted tests",
                    command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("app.py",),
                patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            ),
        )
        runtime = _RecordingFakeRuntime(log=log)
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(
            log,
            [
                "next_action:propose_patch",
                "review_patch",
                "apply_patch",
                "next_action:run_command",
                "execute_command",
                "next_action:complete",
                "finalize_run",
            ],
        )
        self.assertEqual(runtime.calls, ["apply_patch", "execute_command", "finalize_run"])
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(
            [action.type.value for action in outcome.session.action_history],
            ["propose_patch", "run_command", "complete"],
        )
        self.assertEqual(len(outcome.session.prior_artifacts), 1)
        self.assertEqual(outcome.applied_artifacts[0].artifact_type, "patch")
        self.assertEqual(runtime.finalized_status, RunStatus.SUCCEEDED)
        self.assertEqual(runtime.finalized_summary, "Everything succeeded")
        self.assertIsNone(outcome.session.pending_action)

    async def test_coordinator_does_not_apply_patch_when_review_fails(self):
        # Verifies that a rejected patch review stops execution before runtime patch application.
        # This catches coordinators that dispatch unsafe patches even after deterministic review rejects them.
        # The failed outcome is correct because review_patch raised before any patch could be safely applied.
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Unsafe patch",
                    target_files=("app.py",),
                    patch_diff="--- a/../secret.txt\n+++ b/../secret.txt\n@@ -0,0 +1 @@\n+oops\n",
                )
            ],
            review=ValueError("unsafe patch"),
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(runtime.calls, [])
        self.assertEqual(outcome.session.failure_history[-1].stage, "review_patch")
        self.assertIn("unsafe patch", outcome.session.failure_history[-1].message)

    async def test_coordinator_returns_context_request_without_runtime_calls(self):
        # Verifies that ASK_CONTEXT short-circuits execution and returns the requested context directly.
        # This catches accidental runtime dispatch for non-executable context-gathering actions.
        # The context-request outcome is correct because ASK_CONTEXT should not execute any runtime operation.
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.ASK_CONTEXT,
                    reason="Need more files",
                    requested_context=("services/agent_core/local.py",),
                )
            ]
        )
        runtime = _NoRuntimeCalls()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "context_requested")
        self.assertEqual(outcome.requested_context, ("services/agent_core/local.py",))
        self.assertEqual(outcome.session.action_history[0].type, AgentActionType.ASK_CONTEXT)
        self.assertEqual(outcome.session.pending_action.type, AgentActionType.ASK_CONTEXT)

    async def test_coordinator_stops_normal_execution_on_loop_guard_escalation(self):
        # Verifies that loop-guard approval actions stop normal command/patch/finalize dispatch.
        # This catches coordinators that keep executing work even after agent_core escalates the run.
        # The approval outcome is correct because LocalAgentCoreService emits request_approval when the loop guard fires.
        runtime = _RecordingFakeRuntime()
        agent_core = LocalAgentCoreService()
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = agent_core.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Stop runaway loops",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
            ),
            current_plan=AgentPlan(
                goal="Complete the work",
                steps=[
                    AgentStep(
                        kind="patch",
                        description="Update local service",
                        target_files=("services/agent_core/local.py",),
                    )
                ],
            ),
            iteration_count=MAX_AGENT_ITERATIONS,
        )
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "approval_requested")
        self.assertEqual(runtime.calls, ["request_approval"])
        self.assertEqual(outcome.session.action_history[0].type, AgentActionType.REQUEST_APPROVAL)
        self.assertEqual(outcome.session.pending_approval_id, "approval_123")
        self.assertNotIn("execute_command", runtime.calls)
        self.assertNotIn("apply_patch", runtime.calls)
        self.assertNotIn("finalize_run", runtime.calls)

    async def test_coordinator_records_runtime_failures_without_swallowing_them(self):
        # Verifies that runtime execution failures become explicit failed outcomes with recorded agent failure state.
        # This catches coordinators that hide runtime exceptions or continue as if execution succeeded.
        # The failed outcome is correct because the command runtime raised and the coordinator must stop with recorded failure history.
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run failing command",
                    command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
                ),
                AgentAction(type=AgentActionType.COMPLETE, reason="Should not run"),
            ]
        )
        runtime = _FailingRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(runtime.calls, ["execute_command"])
        self.assertEqual(outcome.session.failure_history[-1].stage, "command")
        self.assertIn("runtime command exploded", outcome.session.failure_history[-1].message)
        self.assertEqual([action.type.value for action in outcome.session.action_history], ["run_command"])

    async def test_coordinator_surfaces_structured_runtime_recovery_state(self):
        # Verifies that runtime recovery requirements become explicit needs_recovery outcomes instead of generic failures.
        # This catches coordinators that discard structured recovery metadata and only preserve an exception string.
        # needs_recovery is correct because the runtime reported that the saved task cannot be replayed safely and requires user-visible recovery options.
        recovery_status = RecoveryStatus(
            run_id=new_run_id(),
            task_id="task_recovery",
            recovery_state=RecoveryState.ROLLBACK_AVAILABLE,
            reason="Patch task already started and cannot be replayed safely.",
            recovery_options=(
                RecoveryOption.ROLLBACK_IF_AVAILABLE,
                RecoveryOption.REVIEW_MANUALLY,
                RecoveryOption.ABORT,
            ),
            rollback_task_id="task_recovery",
        )
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run recovering command",
                    command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
                )
            ]
        )
        runtime = _RecoveryRuntime(recovery_status=recovery_status)
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)
        session = replace(self._make_session(), run_id=recovery_status.run_id)

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "needs_recovery")
        self.assertEqual(outcome.session.phase, AgentSessionPhase.NEEDS_RECOVERY)
        self.assertEqual(outcome.recovery_status, recovery_status)
        self.assertEqual(runtime.calls, ["execute_command"])

    async def test_coordinator_marks_exact_step_succeeded_after_runtime_success(self):
        # Verifies that successful execution updates the matching plan step instead of leaving every step pending.
        # This catches the synchronization bug where action_history advanced but current_plan status stayed stale.
        # The expected status is correct because the action names step_2 explicitly, so only that plan step succeeded.
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run targeted tests",
                    step_id="step_2",
                    command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
                ),
                AgentAction(
                    type=AgentActionType.ASK_CONTEXT,
                    reason="Pause after command",
                    requested_context=("services/agent_core/local.py",),
                ),
            ]
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)
        session = self._make_session_with_plan(
            steps=[
                AgentStep(
                    step_id="step_1",
                    kind="patch",
                    description="Update local service",
                    target_files=("services/agent_core/local.py",),
                ),
                AgentStep(
                    step_id="step_2",
                    kind="command",
                    description="Run focused tests",
                    target_files=("tests/unit/test_agent_core_runner.py",),
                ),
            ]
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "context_requested")
        self.assertEqual(outcome.session.action_history[0].step_id, "step_2")
        self.assertEqual(outcome.session.current_plan.steps[0].status, TaskStatus.PENDING)
        self.assertEqual(outcome.session.current_plan.steps[1].status, TaskStatus.SUCCEEDED)

    async def test_coordinator_marks_failed_step_and_records_failure(self):
        # Verifies that a failed runtime action updates both failure history and the matching plan-step status.
        # This catches the bug where command failure was recorded but the plan still showed the step as pending.
        # The expected failed status is correct because the runtime exception means that exact planned command did not succeed.
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run failing command",
                    step_id="step_1",
                    command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
                )
            ]
        )
        runtime = _FailingRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)
        session = self._make_session_with_plan(
            steps=[
                AgentStep(
                    step_id="step_1",
                    kind="command",
                    description="Run focused tests",
                    target_files=("tests/unit/test_agent_core_runner.py",),
                )
            ]
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.session.current_plan.steps[0].status, TaskStatus.FAILED)
        self.assertEqual(outcome.session.failure_history[-1].stage, "command")
        self.assertIn("runtime command exploded", outcome.session.failure_history[-1].message)

    async def test_coordinator_uses_mvp_fallback_for_legacy_actions_without_step_id(self):
        # Verifies the temporary fallback for legacy fabricated actions that do not carry a step_id yet.
        # This catches regressions where old fake/manual actions would never update plan status at all.
        # The expected status is correct because the fallback should update the next compatible pending command step.
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run targeted tests",
                    command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
                ),
                AgentAction(
                    type=AgentActionType.ASK_CONTEXT,
                    reason="Pause after command",
                    requested_context=("services/agent_core/local.py",),
                ),
            ]
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)
        session = self._make_session_with_plan(
            steps=[
                AgentStep(
                    step_id="step_1",
                    kind="patch",
                    description="Update local service",
                    target_files=("services/agent_core/local.py",),
                ),
                AgentStep(
                    step_id="step_2",
                    kind="command",
                    description="Run focused tests",
                    target_files=("tests/unit/test_agent_core_runner.py",),
                ),
            ]
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "context_requested")
        self.assertEqual(outcome.session.current_plan.steps[0].status, TaskStatus.PENDING)
        self.assertEqual(outcome.session.current_plan.steps[1].status, TaskStatus.SUCCEEDED)

    async def test_coordinator_persists_session_after_state_changes(self):
        # Verifies that coordinator-side session mutations are durably saved after action recording and step-status updates.
        # This catches in-memory-only progress where execution changes succeed but the canonical stored snapshot stays stale.
        # The persisted states are correct because the store should see the recorded action, the succeeded command step, the recorded complete action, and the final completed plan.
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run targeted tests",
                    step_id="step_1",
                    command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    step_id="step_2",
                    summary_text="Everything succeeded",
                ),
            ]
        )
        runtime = _RecordingFakeRuntime()
        session_store = _RecordingSessionStore()
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
        )
        session = self._make_session_with_plan(
            steps=[
                AgentStep(
                    step_id="step_1",
                    kind="command",
                    description="Run focused tests",
                    target_files=("tests/unit/test_agent_core_runner.py",),
                ),
                AgentStep(
                    step_id="step_2",
                    kind="complete",
                    description="Finish the run",
                ),
            ]
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "completed")
        self.assertGreaterEqual(len(session_store.saved_sessions), 4)
        self.assertEqual(session_store.saved_sessions[0].action_history[0].type, AgentActionType.RUN_COMMAND)
        self.assertEqual(session_store.saved_sessions[0].pending_action.type, AgentActionType.RUN_COMMAND)
        self.assertEqual(session_store.saved_sessions[0].phase.value, "executing")
        self.assertEqual(session_store.saved_sessions[1].current_plan.steps[0].status, TaskStatus.SUCCEEDED)
        self.assertIsNone(session_store.saved_sessions[1].pending_action)
        self.assertEqual(session_store.saved_sessions[1].phase.value, "ready")
        self.assertEqual(session_store.saved_sessions[-1].current_plan.steps[1].status, TaskStatus.SUCCEEDED)
        self.assertEqual(session_store.saved_sessions[-1].phase.value, "completed")

    async def test_coordinator_persists_pending_command_before_execute_command(self):
        # Verifies that the selected command action is durably saved before the runtime is allowed to execute it.
        # This catches the resume hazard where a side-effecting command could run without a persisted pending_action snapshot.
        # The order is correct because save_agent_session must happen before execute_command to make the command recoverable.
        log: list[str] = []
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run targeted tests",
                    step_id="step_1",
                    command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Finish run",
                    step_id="step_2",
                    summary_text="done",
                ),
            ]
        )
        runtime = _RecordingFakeRuntime(log=log)
        session_store = _RecordingSessionStore(log=log)
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
        )
        session = self._make_session_with_plan(
            steps=[
                AgentStep(step_id="step_1", kind="command", description="Run focused tests"),
                AgentStep(step_id="step_2", kind="complete", description="Finish run"),
            ]
        )

        await coordinator.run(session)

        self.assertLess(log.index("save_agent_session"), log.index("execute_command"))
        self.assertEqual(session_store.saved_sessions[0].pending_action.type, AgentActionType.RUN_COMMAND)

    async def test_coordinator_persists_pending_patch_before_apply_patch(self):
        # Verifies that the selected patch action is durably saved before runtime patch application starts.
        # This catches the resume hazard where a patch side effect could begin without a persisted pending_action snapshot.
        # The order is correct because save_agent_session must happen before apply_patch to make patch replay possible.
        log: list[str] = []
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch app.py",
                    step_id="step_1",
                    target_files=("app.py",),
                    patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Finish run",
                    step_id="step_2",
                    summary_text="done",
                ),
            ],
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("app.py",),
                patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            ),
        )
        runtime = _RecordingFakeRuntime(log=log)
        session_store = _RecordingSessionStore(log=log)
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
        )
        session = self._make_session_with_plan(
            steps=[
                AgentStep(step_id="step_1", kind="patch", description="Patch app.py", target_files=("app.py",)),
                AgentStep(step_id="step_2", kind="complete", description="Finish run"),
            ]
        )

        await coordinator.run(session)

        self.assertLess(log.index("save_agent_session"), log.index("apply_patch"))
        self.assertEqual(session_store.saved_sessions[0].pending_action.type, AgentActionType.PROPOSE_PATCH)

    async def test_runtime_receives_command_task_id_from_pending_action_id(self):
        # Verifies that command dispatch preserves the saved action_id as the runtime task_id.
        # This catches regenerated task ids that would break idempotent replay across resume.
        # The equality is correct because action_id is the stable coordinator-to-runtime idempotency key.
        pending_action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run persisted command",
            step_id="step_1",
            action_id="action_1_run_command_step_1",
            command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
        )
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Finish run",
                    step_id="step_2",
                    summary_text="done",
                )
            ]
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)
        session = self._make_session_with_plan(
            steps=[
                AgentStep(step_id="step_1", kind="command", description="Run persisted command"),
                AgentStep(step_id="step_2", kind="complete", description="Finish run"),
            ],
            action_history=[pending_action],
            pending_action=pending_action,
            iteration_count=1,
        )

        await coordinator.run(session)

        self.assertEqual(runtime.command_requests[0].task_id, pending_action.action_id)

    async def test_runtime_receives_patch_task_id_from_pending_action_id(self):
        # Verifies that patch dispatch preserves the saved action_id as the runtime task_id.
        # This catches regenerated patch task ids that would prevent safe artifact replay after resume.
        # The equality is correct because action_id is the stable idempotency key for the pending patch action.
        pending_action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch persisted file",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
            patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Finish run",
                    step_id="step_2",
                    summary_text="done",
                )
            ],
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("app.py",),
                patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            ),
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)
        session = self._make_session_with_plan(
            steps=[
                AgentStep(step_id="step_1", kind="patch", description="Patch persisted file", target_files=("app.py",)),
                AgentStep(step_id="step_2", kind="complete", description="Finish run"),
            ],
            action_history=[pending_action],
            pending_action=pending_action,
            iteration_count=1,
        )

        await coordinator.run(session)

        self.assertEqual(runtime.patch_proposals[0].task_id, pending_action.action_id)

    async def test_runtime_receives_approval_task_id_from_pending_action_id(self):
        # Verifies that approval dispatch preserves the saved action_id as the runtime task_id.
        # This catches regenerated approval task ids that would allow duplicate approval checkpoints for the same action.
        # The equality is correct because action_id is the stable idempotency key for the approval request.
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.REQUEST_APPROVAL,
                    reason="Need human review",
                    step_id="step_1",
                    action_id="action_1_request_approval_step_1",
                    approval_message="Need human review",
                )
            ]
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)
        session = self._make_session_with_plan(
            steps=[AgentStep(step_id="step_1", kind="approval", description="Need human review")]
        )

        await coordinator.run(session)

        self.assertEqual(runtime.approval_requests[0].task_id, "action_1_request_approval_step_1")

    async def test_coordinator_resumes_pending_command_before_asking_for_next_action(self):
        # Verifies that a persisted pending command is executed first on resume instead of asking agent_core for a fresh action.
        # This catches duplicate planning/model calls on resume that would ignore already-selected work.
        # The order is correct because the saved pending command must be dispatched before the coordinator asks for the next completion step.
        log: list[str] = []
        pending_action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run persisted command",
            step_id="step_1",
            action_id="action_1_run_command_step_1",
            command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
        )
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Finish run",
                    step_id="step_2",
                    summary_text="done",
                )
            ],
        )
        runtime = _RecordingFakeRuntime(log=log)
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)
        session = self._make_session_with_plan(
            steps=[
                AgentStep(step_id="step_1", kind="command", description="Run persisted command"),
                AgentStep(step_id="step_2", kind="complete", description="Finish run"),
            ],
            action_history=[pending_action],
            pending_action=pending_action,
            iteration_count=1,
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(log, ["execute_command", "next_action:complete", "finalize_run"])
        self.assertEqual(outcome.session.current_plan.steps[0].status, TaskStatus.SUCCEEDED)

    async def test_coordinator_does_not_re_request_or_execute_while_approval_is_pending(self):
        # Verifies that a persisted approval checkpoint returns the saved approval state instead of re-requesting or executing work.
        # This catches the bug where reload would ask for approval again or run side effects before approval resolution.
        # The approval outcome is correct because pending_approval_id means the coordinator must wait for explicit approval first.
        pending_action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run risky command",
            step_id="step_1",
            action_id="action_1_run_command_step_1",
            command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
        )
        agent_core = _SequencedFakeAgentCore(actions=[])
        runtime = _NoRuntimeCalls()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)
        session = self._make_session_with_plan(
            steps=[AgentStep(step_id="step_1", kind="command", description="Run risky command")],
            action_history=[pending_action],
            pending_action=pending_action,
            pending_approval_id="approval_123",
            iteration_count=1,
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "approval_requested")
        self.assertEqual(outcome.approval_id, "approval_123")
        self.assertEqual(outcome.last_action.action_id, "action_1_run_command_step_1")

    async def test_resume_after_approval_executes_saved_pending_action(self):
        # Verifies that approval resolution resumes the already-saved pending action rather than asking for a new one first.
        # This catches the bug where approval would bounce back into planning instead of executing the approved work.
        # The order is correct because the saved command is the canonical post-approval work to run.
        log: list[str] = []
        pending_action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run approved command",
            step_id="step_1",
            action_id="action_1_run_command_step_1",
            command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
        )
        stored_session = self._make_session_with_plan(
            steps=[
                AgentStep(step_id="step_1", kind="command", description="Run approved command"),
                AgentStep(step_id="step_2", kind="complete", description="Finish run"),
            ],
            action_history=[pending_action],
            pending_action=pending_action,
            pending_approval_id="approval_123",
            iteration_count=1,
        )
        session_store = _RecordingSessionStore(stored_session)
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Finish run",
                    step_id="step_2",
                    summary_text="done",
                )
            ],
        )
        runtime = _RecordingFakeRuntime(log=log)
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
        )

        outcome = await coordinator.resume_after_approval(
            str(stored_session.run_id),
            approved=True,
            reviewer="human",
            comment="looks safe",
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(
            log,
            ["record_approval_decision", "resume_run", "execute_command", "next_action:complete", "finalize_run"],
        )
        self.assertEqual(outcome.session.current_plan.steps[0].status, TaskStatus.SUCCEEDED)
        self.assertIsNone(outcome.session.pending_approval_id)

    async def test_resume_after_context_merges_context_and_continues(self):
        # Verifies that fulfilling ASK_CONTEXT marks the inspect step complete and then continues normal execution.
        # This catches the bug where added context was ignored and the same context request would repeat on resume.
        # The expected state is correct because the inspect step has been satisfied by the provided repo context.
        pending_action = AgentAction(
            type=AgentActionType.ASK_CONTEXT,
            reason="Need service file",
            step_id="step_1",
            action_id="action_1_ask_context_step_1",
            requested_context=("services/agent_core/local.py",),
        )
        stored_session = self._make_session_with_plan(
            steps=[
                AgentStep(step_id="step_1", kind="inspect", description="Need service file"),
                AgentStep(step_id="step_2", kind="complete", description="Finish run"),
            ],
            action_history=[pending_action],
            pending_action=pending_action,
            iteration_count=1,
        )
        session_store = _RecordingSessionStore(stored_session)
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Finish run",
                    step_id="step_2",
                    summary_text="done",
                )
            ],
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
        )
        refreshed_context = RepoContextResult(
            workspace_id=stored_session.workspace_id,
            run_id=stored_session.run_id,
            repo_map="services/\n  agent_core/\n  execution_runtime/\n",
        )

        outcome = await coordinator.resume_after_context(str(stored_session.run_id), refreshed_context)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.session.repo_context.repo_map, refreshed_context.repo_map)
        self.assertEqual(outcome.session.current_plan.steps[0].status, TaskStatus.SUCCEEDED)
        self.assertIsNone(outcome.session.pending_action)

    async def test_resume_pending_patch_replays_existing_runtime_artifact_without_reapplying(self):
        # Verifies that resuming a saved pending patch reuses the existing runtime artifact instead of applying the patch again.
        # This catches duplicate patch side effects after resume when the pending action already succeeded durably in runtime.
        # Replay is correct because the runtime already has an artifact for the same action_id/task_id pair.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            runtime, repository = self._make_runtime(root, workspace)
            run_id = await runtime.enqueue_run(self._make_run_request(workspace))
            await runtime.claim_next_run("worker-a", lease_seconds=30)

            patch_diff = "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-before\n+after\n"
            file_path = root / "app.txt"
            file_path.write_text("before\n", encoding="utf-8")
            action_id = "action_1_propose_patch_step_1"

            await runtime.apply_patch(
                run_id,
                PatchProposal(
                    run_id=run_id,
                    artifact_id="artifact_patch_1",
                    task_id=action_id,
                    summary="Patch persisted file",
                    unified_diff=patch_diff,
                    target_paths=("app.txt",),
                ),
            )

            pending_action = AgentAction(
                type=AgentActionType.PROPOSE_PATCH,
                reason="Patch persisted file",
                step_id="step_1",
                action_id=action_id,
                target_files=("app.txt",),
                patch_diff=patch_diff,
            )
            stored_session = replace(
                self._make_session_with_plan(
                steps=[
                    AgentStep(step_id="step_1", kind="patch", description="Patch persisted file", target_files=("app.txt",)),
                    AgentStep(step_id="step_2", kind="complete", description="Finish run"),
                ],
                action_history=[pending_action],
                pending_action=pending_action,
                iteration_count=1,
                ),
                run_id=run_id,
                workspace_id=workspace.workspace_id,
            )
            await repository.save_agent_session(stored_session)
            agent_core = _SequencedFakeAgentCore(
                actions=[
                    AgentAction(
                        type=AgentActionType.COMPLETE,
                        reason="Finish run",
                        step_id="step_2",
                        summary_text="done",
                    )
                ],
                review=PatchReview(
                    accepted=True,
                    reason="Patch passed review",
                    changed_files=("app.txt",),
                    patch_diff=patch_diff,
                ),
            )
            coordinator = AgentCoreCoordinator(
                agent_core=agent_core,
                execution_runtime=runtime,
                session_store=repository,
            )

            outcome = await coordinator.resume(run_id)
            artifacts = await repository.list_artifacts(run_id)
            events = await repository.list_events(run_id)

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(file_path.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(len(artifacts), 1)
            self.assertEqual([event.event_type for event in events].count(EventType.PATCH_APPLIED), 1)
            self.assertEqual(
                [
                    event
                    for event in events
                    if event.event_type == EventType.AGENT_MESSAGE and event.payload.get("kind") == "patch.started"
                ].__len__(),
                1,
            )

    async def test_resume_with_pending_approval_does_not_create_duplicate_approval_request(self):
        # Verifies that reloading an already-suspended approval checkpoint does not create a second durable approval request.
        # This catches duplicate approval side effects when resume sees the same pending approval more than once.
        # One approval row is correct because the saved pending_approval_id already represents the durable checkpoint for that action.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            runtime, repository = self._make_runtime(root, workspace)
            run_id = await runtime.enqueue_run(self._make_run_request(workspace))
            await runtime.claim_next_run("worker-a", lease_seconds=30)

            agent_core = _SequencedFakeAgentCore(
                actions=[
                    AgentAction(
                        type=AgentActionType.REQUEST_APPROVAL,
                        reason="Need human review",
                        step_id="step_1",
                        action_id="action_1_request_approval_step_1",
                        approval_message="Need human review",
                    )
                ]
            )
            coordinator = AgentCoreCoordinator(
                agent_core=agent_core,
                execution_runtime=runtime,
                session_store=repository,
            )
            session = replace(
                self._make_session_with_plan(
                    steps=[AgentStep(step_id="step_1", kind="approval", description="Need human review")]
                ),
                run_id=run_id,
                workspace_id=workspace.workspace_id,
            )

            first = await coordinator.run(session)
            approvals_after_first = await repository.list_approval_requests(run_id)
            second = await coordinator.resume(run_id)
            approvals_after_second = await repository.list_approval_requests(run_id)

            self.assertEqual(first.status, "approval_requested")
            self.assertEqual(second.status, "approval_requested")
            self.assertEqual(len(approvals_after_first), 1)
            self.assertEqual(len(approvals_after_second), 1)


if __name__ == "__main__":
    unittest.main()
