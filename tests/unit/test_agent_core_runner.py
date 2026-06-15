from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from packages.shared_types import (
    ArtifactRef,
    CommandResult,
    EventType,
    FileSummary,
    ImpactAnalysis,
    PatchProposal,
    RecoveryOption,
    RecoveryState,
    RecoveryStatus,
    RepoContextResult,
    RunRequest,
    Session,
    RunStatus,
    SymbolMatch,
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
    AgentVerification,
)
from services.agent_core.validation import AgentPatchGenerationError, AgentPatchReviewError
from services.agent_core.validation import MAX_AGENT_ITERATIONS
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class _SequencedFakeAgentCore:
    def __init__(
        self,
        *,
        actions: list[AgentAction],
        generated_command: AgentAction | Exception | None = None,
        generated_patch: AgentAction | Exception | None = None,
        planned_verification: (
            AgentVerification
            | tuple[AgentVerification, ...]
            | list[AgentVerification | tuple[AgentVerification, ...] | None]
            | None
        ) = None,
        review: PatchReview | Exception | list[PatchReview | Exception] | None = None,
        log: list[str] | None = None,
    ) -> None:
        self._actions = list(actions)
        self._generated_command = generated_command
        self._generated_patch = generated_patch
        self._planned_verification = planned_verification
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

    async def generate_patch(self, session, proposed_action):
        self._log.append("generate_patch")
        generated_patch = self._generated_patch
        if isinstance(generated_patch, list):
            if not generated_patch:
                raise AssertionError("No more fake generated_patch responses available")
            generated_patch = generated_patch.pop(0)
        if isinstance(generated_patch, Exception):
            raise generated_patch
        if generated_patch is not None:
            return generated_patch
        return proposed_action

    async def generate_command(self, session, proposed_action):
        self._log.append("generate_command")
        if isinstance(self._generated_command, Exception):
            raise self._generated_command
        if self._generated_command is not None:
            return self._generated_command
        return proposed_action

    async def review_patch(self, session, proposed_action):
        self._log.append("review_patch")
        review = self._review
        if isinstance(review, list):
            if not review:
                raise AssertionError("No more fake review responses available")
            review = review.pop(0)
        if isinstance(review, Exception):
            raise review
        return review

    def plan_patch_verification(self, session, *, changed_files, deleted_files=(), workspace_root=None):
        del session, changed_files, deleted_files, workspace_root
        self._log.append("plan_patch_verification")
        planned = self._planned_verification
        if isinstance(planned, list):
            if not planned:
                raise AssertionError("No more fake planned_verification responses available")
            planned = planned.pop(0)
        if planned is None:
            return ()
        if isinstance(planned, AgentVerification):
            return (planned,)
        return planned

    async def summarize_run(self, session):
        self._log.append("summarize_run")
        return RunSummary(final_status="completed")


class _RecordingFakeRuntime:
    def __init__(
        self,
        *,
        command_results: list[CommandResult] | None = None,
        log: list[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.finalized_status: RunStatus | None = None
        self.finalized_summary: str | None = None
        self._log = log if log is not None else []
        self.recorded_decisions: list[tuple[bool, str | None, str | None]] = []
        self.command_requests = []
        self.patch_proposals = []
        self.approval_requests = []
        self._command_results = list(command_results) if command_results is not None else None

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
        if self._command_results is not None:
            if not self._command_results:
                raise AssertionError("No more fake command results available")
            result = self._command_results.pop(0)
            return replace(result, run_id=request.run_id, task_id=request.task_id)
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


class _RecordingRepoIntelligence:
    def __init__(
        self,
        *,
        refreshed_context: RepoContextResult,
        impact_analysis: ImpactAnalysis | None = None,
        symbol_matches: tuple[SymbolMatch, ...] = (),
        fail_stage: str | None = None,
        log: list[str] | None = None,
    ) -> None:
        self.refreshed_context = refreshed_context
        self.impact_analysis = impact_analysis or ImpactAnalysis()
        self.symbol_matches = symbol_matches
        self.fail_stage = fail_stage
        self.log = log if log is not None else []
        self.refresh_calls: list[tuple[Workspace, tuple[str, ...]]] = []
        self.impact_calls: list[tuple[Workspace, tuple[str, ...]]] = []
        self.search_queries: list[str] = []
        self.context_requests = []

    async def refresh_index(self, workspace, changed_files):
        self.log.append("refresh_index")
        self.refresh_calls.append((workspace, tuple(changed_files)))
        if self.fail_stage == "refresh_index":
            raise RuntimeError("refresh_index failed")

    async def analyze_impact(self, workspace, files):
        self.log.append("analyze_impact")
        self.impact_calls.append((workspace, tuple(files)))
        if self.fail_stage == "analyze_impact":
            raise RuntimeError("analyze_impact failed")
        return self.impact_analysis

    async def search_symbols(self, workspace, query):
        self.log.append("search_symbols")
        self.search_queries.append(query)
        if self.fail_stage == "search_symbols":
            raise RuntimeError("search_symbols failed")
        return self.symbol_matches

    async def build_context(self, request):
        self.log.append("build_context")
        self.context_requests.append(request)
        if self.fail_stage == "build_context":
            raise RuntimeError("build_context failed")
        return self.refreshed_context


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
        repo_context = overrides.pop(
            "repo_context",
            RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
            ),
        )
        return service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Run the coordinator deterministically",
            repo_context=repo_context,
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
                "plan_patch_verification",
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

    async def test_coordinator_generates_command_before_execute_command(self):
        # Verifies that command execution now goes through generate_command before runtime dispatch when next_action only returns command intent.
        # This catches the broken command chain where run_command reached dispatch without a concrete argv payload.
        # The order is correct because the coordinator must first ask agent_core to generate argv, then persist it, then execute the command.
        log: list[str] = []
        command_action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run targeted tests",
            step_id="step_1",
            action_id="action_1_run_command_step_1",
            target_files=("tests/unit/test_agent_core_runner.py",),
        )
        generated_command = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run targeted tests",
            step_id="step_1",
            action_id="action_1_run_command_step_1",
            target_files=("tests/unit/test_agent_core_runner.py",),
            command_argv=("python", "-m", "unittest", "tests.unit.test_agent_core_runner"),
            cwd=".",
        )
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                command_action,
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            generated_command=generated_command,
        )
        runtime = _RecordingFakeRuntime(log=log)
        session_store = _RecordingSessionStore(log=log)
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
        )

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(
            log,
            [
                "next_action:run_command",
                "save_agent_session",
                "generate_command",
                "save_agent_session",
                "execute_command",
                "save_agent_session",
                "next_action:complete",
                "save_agent_session",
                "finalize_run",
                "save_agent_session",
            ],
        )
        self.assertEqual(
            session_store.saved_sessions[1].pending_action.command_argv,
            generated_command.command_argv,
        )
        self.assertEqual(runtime.command_requests[0].argv, generated_command.command_argv)

    async def test_coordinator_generates_patch_diff_before_review_and_apply(self):
        # Verifies that patch execution now goes through generate_patch before review/apply when next_action only returns patch intent.
        # This catches the broken patch chain where propose_patch reached dispatch without a patch_diff.
        # The order is correct because the coordinator must first ask agent_core to generate a concrete diff, then review it, then apply it.
        log: list[str] = []
        patch_action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch app.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
        )
        generated_patch = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch app.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
            patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                patch_action,
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            generated_patch=generated_patch,
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("app.py",),
                patch_diff=generated_patch.patch_diff,
            ),
        )
        runtime = _RecordingFakeRuntime(log=log)
        session_store = _RecordingSessionStore(log=log)
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
        )

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(
            log,
            [
                "next_action:propose_patch",
                "save_agent_session",
                "generate_patch",
                "save_agent_session",
                "review_patch",
                "apply_patch",
                "plan_patch_verification",
                "save_agent_session",
                "next_action:complete",
                "save_agent_session",
                "finalize_run",
                "save_agent_session",
            ],
        )
        self.assertEqual(session_store.saved_sessions[1].pending_action.patch_diff, generated_patch.patch_diff)
        self.assertEqual(runtime.patch_proposals[0].unified_diff, generated_patch.patch_diff)

    async def test_coordinator_refreshes_repo_context_after_successful_patch(self):
        # Verifies that a successful patch refreshes repo intelligence and stores the refreshed context for later agent steps.
        # This catches stale post-edit session context, which previously left later prompts anchored to pre-patch file contents.
        # The refreshed context is correct because the coordinator should update repo intelligence after the runtime mutates workspace files.
        log: list[str] = []
        workspace = Workspace(root_path="/tmp/agent-core-runner-refresh")
        run_id = new_run_id()
        refreshed_context = RepoContextResult(
            workspace_id=workspace.workspace_id,
            run_id=run_id,
            repo_map="updated repo map",
            file_summaries=(
                FileSummary(path="app.py", summary="after patch", content="after\n"),
                FileSummary(path="tests/test_app.py", summary="impacted test"),
            ),
            warnings=("context warning",),
        )
        repo_intelligence = _RecordingRepoIntelligence(
            refreshed_context=refreshed_context,
            impact_analysis=ImpactAnalysis(
                changed_paths=("app.py",),
                impacted_paths=("app.py", "tests/test_app.py"),
                warnings=("impact warning",),
            ),
            log=log,
        )
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch app.py",
                    step_id="step_1",
                    action_id="action_1_propose_patch_step_1",
                    target_files=("app.py",),
                    patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-before\n+after\n",
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
                patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-before\n+after\n",
            ),
        )
        runtime = _RecordingFakeRuntime(log=log)
        session_store = _RecordingSessionStore(log=log)
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
            repo_intelligence=repo_intelligence,
            repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
        )
        service = LocalAgentCoreService()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace.workspace_id,
            user_request="Patch app.py and continue",
            repo_context=RepoContextResult(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                repo_map="stale repo map",
                file_summaries=(
                    FileSummary(path="app.py", summary="before patch", content="before\n"),
                ),
            ),
            current_plan=AgentPlan(
                goal="Patch and finish",
                steps=[
                    AgentStep(step_id="step_1", kind="patch", description="Patch app.py", target_files=("app.py",)),
                    AgentStep(step_id="step_2", kind="complete", description="Finish"),
                ],
            ),
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(log, [
            "next_action:propose_patch",
            "save_agent_session",
            "review_patch",
            "apply_patch",
            "refresh_index",
            "analyze_impact",
            "build_context",
            "plan_patch_verification",
            "save_agent_session",
            "next_action:complete",
            "save_agent_session",
            "finalize_run",
            "save_agent_session",
        ])
        self.assertEqual(repo_intelligence.refresh_calls[0][1], ("app.py",))
        self.assertEqual(repo_intelligence.impact_calls[0][1], ("app.py",))
        self.assertEqual(repo_intelligence.context_requests[0].target_paths, ("app.py", "tests/test_app.py"))
        self.assertEqual(outcome.session.repo_context.repo_map, "updated repo map")
        self.assertEqual(outcome.session.repo_context.file_summaries[0].content, "after\n")
        self.assertEqual(
            outcome.session.repo_context.warnings,
            ("impact warning", "context warning"),
        )
        self.assertIn("impact warning", outcome.session.warnings)
        self.assertIn("context warning", outcome.session.warnings)

    async def test_coordinator_keeps_patch_run_alive_when_repo_refresh_fails(self):
        # Verifies that repo-intelligence refresh is best-effort after patch application.
        # This catches a brittle coordinator that would turn a successful runtime patch into a failed run just because index refresh broke.
        # The run still completes because execution side effects already succeeded and refresh should only annotate warnings.
        workspace = Workspace(root_path="/tmp/agent-core-runner-refresh-failure")
        run_id = new_run_id()
        repo_intelligence = _RecordingRepoIntelligence(
            refreshed_context=RepoContextResult(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
            ),
            fail_stage="refresh_index",
        )
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch app.py",
                    step_id="step_1",
                    action_id="action_1_propose_patch_step_1",
                    target_files=("app.py",),
                    patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-before\n+after\n",
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
                patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-before\n+after\n",
            ),
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            repo_intelligence=repo_intelligence,
            repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
        )
        service = LocalAgentCoreService()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace.workspace_id,
            user_request="Patch app.py and finish",
            repo_context=RepoContextResult(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                file_summaries=(FileSummary(path="app.py", summary="before patch", content="before\n"),),
            ),
            current_plan=AgentPlan(
                goal="Patch and finish",
                steps=[
                    AgentStep(step_id="step_1", kind="patch", description="Patch app.py", target_files=("app.py",)),
                    AgentStep(step_id="step_2", kind="complete", description="Finish"),
                ],
            ),
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "completed")
        self.assertIn("repo_intelligence refresh after patch failed", outcome.session.warnings[0])

    async def test_successful_patch_triggers_automatic_py_compile_verification(self):
        # Verifies that a successful patch automatically runs deterministic Python syntax verification for changed Python files.
        # This catches regressions where post-patch verification is skipped and syntax errors are only discovered in later manual checks.
        # The py_compile argv is correct because this phase only compiles the changed Python files and uses a fixed deterministic command.
        log: list[str] = []
        run_id = new_run_id()
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch Python files",
                    step_id="step_1",
                    action_id="action_1_propose_patch_step_1",
                    target_files=("app.py", "pkg/module.py"),
                    patch_diff=(
                        "--- a/app.py\n"
                        "+++ b/app.py\n"
                        "@@ -1 +1 @@\n"
                        "-old\n"
                        "+new\n"
                        "--- a/pkg/module.py\n"
                        "+++ b/pkg/module.py\n"
                        "@@ -1 +1 @@\n"
                        "-old\n"
                        "+new\n"
                    ),
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            planned_verification=AgentVerification(
                command_argv=("python", "-m", "py_compile", "app.py", "pkg/module.py"),
                changed_files=("app.py", "pkg/module.py"),
            ),
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("app.py", "pkg/module.py"),
                patch_diff=(
                    "--- a/app.py\n"
                    "+++ b/app.py\n"
                    "@@ -1 +1 @@\n"
                    "-old\n"
                    "+new\n"
                    "--- a/pkg/module.py\n"
                    "+++ b/pkg/module.py\n"
                    "@@ -1 +1 @@\n"
                    "-old\n"
                    "+new\n"
                ),
            ),
        )
        runtime = _RecordingFakeRuntime(
            command_results=[
                CommandResult(run_id=run_id, task_id="task_verify", exit_code=0, stdout=""),
            ],
            log=log,
        )
        session_store = _RecordingSessionStore(log=log)
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
        )

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(runtime.calls, ["apply_patch", "execute_command", "finalize_run"])
        self.assertEqual(
            runtime.command_requests[0].argv,
            ("python", "-m", "py_compile", "app.py", "pkg/module.py"),
        )
        self.assertEqual(
            outcome.session.verification_history[-1].changed_files,
            ("app.py", "pkg/module.py"),
        )
        self.assertEqual(outcome.session.verification_history[-1].exit_code, 0)
        self.assertEqual(outcome.session.verification_history[-1].verification_level, "syntax_only")

    async def test_successful_patch_runs_targeted_pytest_after_syntax_verification(self):
        # Verifies that deterministic targeted tests run only after syntax verification succeeds.
        # This catches a regression where py_compile would pass but the related focused unit test would never execute.
        # The two-command sequence is correct because this phase should run syntax verification first, then a single allowlisted pytest command for the matched test file.
        log: list[str] = []
        run_id = new_run_id()
        patch_diff = "--- a/services/agent_core/runner.py\n+++ b/services/agent_core/runner.py\n@@ -1 +1 @@\n-old\n+new\n"
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch runner.py",
                    step_id="step_1",
                    action_id="action_1_propose_patch_step_1",
                    target_files=("services/agent_core/runner.py",),
                    patch_diff=patch_diff,
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            planned_verification=(
                AgentVerification(
                    verification_level="syntax_only",
                    command_argv=("python", "-m", "py_compile", "services/agent_core/runner.py"),
                    changed_files=("services/agent_core/runner.py",),
                ),
                AgentVerification(
                    kind="targeted_pytest",
                    verification_level="targeted_tests_passed",
                    command_argv=("python", "-m", "pytest", "tests/unit/test_agent_core_runner.py"),
                    changed_files=("services/agent_core/runner.py",),
                ),
            ),
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("services/agent_core/runner.py",),
                patch_diff=patch_diff,
            ),
        )
        runtime = _RecordingFakeRuntime(
            command_results=[
                CommandResult(run_id=run_id, task_id="task_syntax", exit_code=0),
                CommandResult(run_id=run_id, task_id="task_targeted", exit_code=0, stdout="1 passed"),
            ],
            log=log,
        )
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(
            runtime.calls,
            ["apply_patch", "execute_command", "execute_command", "finalize_run"],
        )
        self.assertEqual(
            [request.argv for request in runtime.command_requests],
            [
                ("python", "-m", "py_compile", "services/agent_core/runner.py"),
                ("python", "-m", "pytest", "tests/unit/test_agent_core_runner.py"),
            ],
        )
        self.assertEqual(
            [item.verification_level for item in outcome.session.verification_history],
            ["syntax_only", "targeted_tests_passed"],
        )

    async def test_patch_without_matching_test_records_functional_verification_missing(self):
        # Verifies that syntax verification without any matched focused test does not claim full functional coverage.
        # This catches an overconfident success state where py_compile alone would be treated as equivalent to targeted test validation.
        # Recording a non-command functional-verification gap is correct because the syntax check passed but no deterministic test file was found.
        run_id = new_run_id()
        patch_diff = "--- a/docs/readme_helper.py\n+++ b/docs/readme_helper.py\n@@ -1 +1 @@\n-old\n+new\n"
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch helper without focused tests",
                    step_id="step_1",
                    action_id="action_1_propose_patch_step_1",
                    target_files=("docs/readme_helper.py",),
                    patch_diff=patch_diff,
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            planned_verification=(
                AgentVerification(
                    verification_level="syntax_only",
                    command_argv=("python", "-m", "py_compile", "docs/readme_helper.py"),
                    changed_files=("docs/readme_helper.py",),
                ),
                AgentVerification(
                    kind="functional_verification_missing",
                    verification_level="functional_verification_missing",
                    changed_files=("docs/readme_helper.py",),
                ),
            ),
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("docs/readme_helper.py",),
                patch_diff=patch_diff,
            ),
        )
        runtime = _RecordingFakeRuntime(
            command_results=[
                CommandResult(run_id=run_id, task_id="task_syntax", exit_code=0),
            ],
        )
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(runtime.calls, ["apply_patch", "execute_command", "finalize_run"])
        self.assertEqual(len(outcome.session.verification_history), 2)
        self.assertEqual(
            [item.verification_level for item in outcome.session.verification_history],
            ["syntax_only", "functional_verification_missing"],
        )
        self.assertEqual(outcome.session.verification_history[1].command_argv, ())

    async def test_configured_fallback_runs_when_functional_verification_is_missing(self):
        # Verifies that an explicit allowlisted fallback test command runs after syntax verification when no deterministic targeted test was found.
        # This catches the gap where legacy-style explicit test configuration would be ignored and the run would stop at syntax_only.
        # The verification sequence is correct because syntax must run first and the configured fallback pytest command is the only functional check available.
        run_id = new_run_id()
        patch_diff = "--- a/docs/readme_helper.py\n+++ b/docs/readme_helper.py\n@@ -1 +1 @@\n-old\n+new\n"
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch helper without targeted tests",
                    step_id="step_1",
                    action_id="action_1_propose_patch_step_1",
                    target_files=("docs/readme_helper.py",),
                    patch_diff=patch_diff,
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            planned_verification=(
                AgentVerification(
                    verification_level="syntax_only",
                    command_argv=("python", "-m", "py_compile", "docs/readme_helper.py"),
                    changed_files=("docs/readme_helper.py",),
                ),
                AgentVerification(
                    kind="fallback_pytest",
                    verification_level="fallback_tests_passed",
                    command_argv=("python", "-m", "pytest", "tests/unit"),
                    changed_files=("docs/readme_helper.py",),
                ),
            ),
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("docs/readme_helper.py",),
                patch_diff=patch_diff,
            ),
        )
        runtime = _RecordingFakeRuntime(
            command_results=[
                CommandResult(run_id=run_id, task_id="task_syntax", exit_code=0),
                CommandResult(run_id=run_id, task_id="task_fallback", exit_code=0, stdout="2 passed"),
            ],
        )
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(
            [request.argv for request in runtime.command_requests],
            [
                ("python", "-m", "py_compile", "docs/readme_helper.py"),
                ("python", "-m", "pytest", "tests/unit"),
            ],
        )
        self.assertEqual(
            [item.verification_level for item in outcome.session.verification_history],
            ["syntax_only", "fallback_tests_passed"],
        )

    async def test_disallowed_fallback_is_rejected_without_runtime_execution(self):
        # Verifies that an invalid explicit fallback command is rejected before reaching the execution runtime.
        # This catches a dangerous regression where arbitrary configured commands could bypass the new allowlist through the verification path.
        # Skipping execution is correct because the configured command is missing a pytest target path and therefore is not an allowed fallback form.
        run_id = new_run_id()
        patch_diff = "--- a/docs/readme_helper.py\n+++ b/docs/readme_helper.py\n@@ -1 +1 @@\n-old\n+new\n"
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch helper without targeted tests",
                    step_id="step_1",
                    action_id="action_1_propose_patch_step_1",
                    target_files=("docs/readme_helper.py",),
                    patch_diff=patch_diff,
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            planned_verification=(
                AgentVerification(
                    verification_level="syntax_only",
                    command_argv=("python", "-m", "py_compile", "docs/readme_helper.py"),
                    changed_files=("docs/readme_helper.py",),
                ),
                AgentVerification(
                    kind="fallback_pytest",
                    verification_level="fallback_tests_passed",
                    command_argv=("python", "-m", "pytest"),
                    changed_files=("docs/readme_helper.py",),
                ),
            ),
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("docs/readme_helper.py",),
                patch_diff=patch_diff,
            ),
        )
        runtime = _RecordingFakeRuntime(
            command_results=[
                CommandResult(run_id=run_id, task_id="task_syntax", exit_code=0),
            ],
        )
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(runtime.calls, ["apply_patch", "execute_command", "finalize_run"])
        self.assertEqual(len(outcome.session.verification_history), 2)
        self.assertEqual(outcome.session.verification_history[1].kind, "fallback_pytest_rejected")
        self.assertEqual(
            outcome.session.verification_history[1].verification_level,
            "functional_verification_missing",
        )
        self.assertIn("Fallback test command rejected", outcome.session.warnings[-1])

    async def test_fallback_failure_reuses_repair_loop(self):
        # Verifies that an explicit fallback pytest failure enters the same repair loop as other verification failures.
        # This catches a split path where fallback tests would fail but never feed stderr back into repo_intelligence and repair generation.
        # The repair sequence is correct because the agent should rerun syntax verification and the same fallback command after applying a fix.
        workspace = Workspace(root_path="/tmp/agent-core-runner-fallback-test")
        run_id = new_run_id()
        patch_diff = "--- a/docs/readme_helper.py\n+++ b/docs/readme_helper.py\n@@ -1 +1 @@\n-old\n+new\n"
        repo_intelligence = _RecordingRepoIntelligence(
            refreshed_context=RepoContextResult(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                repo_map="refreshed repo map",
                file_summaries=(FileSummary(path="docs/readme_helper.py", summary="repair target", content="def ok():\n    return 1\n"),),
            ),
            impact_analysis=ImpactAnalysis(
                changed_paths=("docs/readme_helper.py",),
                impacted_paths=("docs/readme_helper.py",),
            ),
        )
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch helper without targeted tests",
                    step_id="step_1",
                    action_id="action_1_propose_patch_step_1",
                    target_files=("docs/readme_helper.py",),
                    patch_diff=patch_diff,
                ),
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Repair helper after fallback failure",
                    step_id="step_2",
                    action_id="action_2_propose_patch_step_2",
                    target_files=("docs/readme_helper.py",),
                    patch_diff=patch_diff,
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            planned_verification=[
                (
                    AgentVerification(
                        verification_level="syntax_only",
                        command_argv=("python", "-m", "py_compile", "docs/readme_helper.py"),
                        changed_files=("docs/readme_helper.py",),
                    ),
                    AgentVerification(
                        kind="fallback_pytest",
                        verification_level="fallback_tests_passed",
                        command_argv=("python", "-m", "pytest", "tests/unit"),
                        changed_files=("docs/readme_helper.py",),
                    ),
                ),
                (
                    AgentVerification(
                        verification_level="syntax_only",
                        command_argv=("python", "-m", "py_compile", "docs/readme_helper.py"),
                        changed_files=("docs/readme_helper.py",),
                    ),
                    AgentVerification(
                        kind="fallback_pytest",
                        verification_level="fallback_tests_passed",
                        command_argv=("python", "-m", "pytest", "tests/unit"),
                        changed_files=("docs/readme_helper.py",),
                    ),
                ),
            ],
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("docs/readme_helper.py",),
                patch_diff=patch_diff,
            ),
        )
        runtime = _RecordingFakeRuntime(
            command_results=[
                CommandResult(run_id=run_id, task_id="task_syntax_1", exit_code=0),
                CommandResult(run_id=run_id, task_id="task_fallback_1", exit_code=1, stderr="AssertionError: fallback failed"),
                CommandResult(run_id=run_id, task_id="task_syntax_2", exit_code=0),
                CommandResult(run_id=run_id, task_id="task_fallback_2", exit_code=0, stdout="2 passed"),
            ],
        )
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=_RecordingSessionStore(),
            repo_intelligence=repo_intelligence,
            repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
        )
        session = LocalAgentCoreService().create_session(
            run_id=run_id,
            workspace_id=workspace.workspace_id,
            user_request="Patch helper and use configured fallback tests",
            repo_context=RepoContextResult(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                repo_map="stale repo map",
                file_summaries=(FileSummary(path="docs/readme_helper.py", summary="before patch", content="old\n"),),
            ),
            current_plan=AgentPlan(
                goal="Patch and verify",
                steps=[
                    AgentStep(step_id="step_1", kind="patch", description="Patch helper", target_files=("docs/readme_helper.py",)),
                    AgentStep(step_id="step_2", kind="patch", description="Repair helper", target_files=("docs/readme_helper.py",)),
                    AgentStep(step_id="step_3", kind="complete", description="Finish"),
                ],
            ),
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "completed")
        failed_verification = next(
            item for item in outcome.session.verification_history if item.exit_code == 1
        )
        self.assertEqual(failed_verification.verification_level, "fallback_tests_failed")
        self.assertFalse(any(failure.stage == "command" for failure in outcome.session.failure_history))

    async def test_coordinator_records_command_failure_context_and_continues_repair_loop(self):
        # Verifies that nonzero command exits are recorded as retryable failures, enriched with repo intelligence, and kept in-loop for repair.
        # This catches the old broken behavior where verification failures stopped the run without feeding stderr/stdout and refreshed context into the next fix step.
        # The completed outcome is correct because the coordinator should repair after the failed command, rerun verification, and only then finish.
        log: list[str] = []
        workspace = Workspace(root_path="/tmp/agent-core-runner-command-repair")
        run_id = new_run_id()
        repo_intelligence = _RecordingRepoIntelligence(
            refreshed_context=RepoContextResult(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                repo_map="refreshed repo map",
                file_summaries=(
                    FileSummary(path="app.py", summary="helper definition", content="helper_value = 1\n"),
                    FileSummary(path="tests/test_app.py", summary="failing test"),
                ),
                warnings=("context warning",),
            ),
            impact_analysis=ImpactAnalysis(
                changed_paths=("tests/test_app.py",),
                impacted_paths=("app.py", "tests/test_app.py"),
                warnings=("impact warning",),
            ),
            symbol_matches=(
                SymbolMatch(name="helper_value", kind="variable", path="app.py", line=3),
            ),
            log=log,
        )
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run focused tests",
                    step_id="step_1",
                    action_id="action_1_run_command_step_1",
                    target_files=("tests/test_app.py",),
                    command_argv=("python", "-m", "pytest", "tests/test_app.py"),
                ),
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Fix the helper usage",
                    step_id="step_2",
                    action_id="action_2_propose_patch_step_2",
                    target_files=("app.py",),
                    patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-helper_value = 0\n+helper_value = 1\n",
                ),
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Re-run focused tests",
                    step_id="step_1",
                    action_id="action_3_run_command_step_1",
                    target_files=("tests/test_app.py",),
                    command_argv=("python", "-m", "pytest", "tests/test_app.py"),
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
                patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-helper_value = 0\n+helper_value = 1\n",
            ),
        )
        runtime = _RecordingFakeRuntime(
            command_results=[
                CommandResult(
                    run_id=run_id,
                    task_id="task_fail",
                    exit_code=1,
                    stdout="F",
                    stderr="NameError: helper_value is not defined",
                ),
                CommandResult(
                    run_id=run_id,
                    task_id="task_pass",
                    exit_code=0,
                    stdout="1 passed",
                ),
            ],
            log=log,
        )
        session_store = _RecordingSessionStore(log=log)
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
            repo_intelligence=repo_intelligence,
            repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
        )
        session = LocalAgentCoreService().create_session(
            run_id=run_id,
            workspace_id=workspace.workspace_id,
            user_request="Fix the failing test and rerun it",
            repo_context=RepoContextResult(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                repo_map="stale repo map",
                file_summaries=(
                    FileSummary(path="tests/test_app.py", summary="failing test", content="assert helper_value == 1\n"),
                ),
            ),
            current_plan=AgentPlan(
                goal="Fix the failure",
                steps=[
                    AgentStep(step_id="step_1", kind="command", description="Run focused tests", target_files=("tests/test_app.py",)),
                    AgentStep(step_id="step_2", kind="patch", description="Fix the helper usage", target_files=("app.py",)),
                    AgentStep(step_id="step_3", kind="complete", description="Finish"),
                ],
            ),
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(
            runtime.calls,
            ["execute_command", "apply_patch", "execute_command", "finalize_run"],
        )
        self.assertIn("search_symbols", log)
        self.assertEqual(repo_intelligence.search_queries[0], "NameError")
        self.assertEqual(repo_intelligence.impact_calls[0][1], ("tests/test_app.py",))
        self.assertEqual(
            repo_intelligence.context_requests[0].target_paths,
            ("tests/test_app.py", "app.py"),
        )
        self.assertIn("Verification command failed", repo_intelligence.context_requests[0].prompt or "")
        self.assertIn(
            "stderr: NameError: helper_value is not defined",
            repo_intelligence.context_requests[0].prompt or "",
        )

        failure_snapshot = session_store.saved_sessions[1]
        self.assertEqual(failure_snapshot.failure_history[-1].stage, "command")
        self.assertTrue(failure_snapshot.failure_history[-1].retryable)
        self.assertEqual(failure_snapshot.failure_history[-1].code, "command_failed")
        self.assertEqual(
            failure_snapshot.failure_history[-1].details["stderr"],
            "NameError: helper_value is not defined",
        )
        self.assertEqual(failure_snapshot.failure_history[-1].details["stdout"], "F")
        self.assertEqual(failure_snapshot.repo_context.repo_map, "refreshed repo map")
        self.assertEqual(
            tuple(match.name for match in failure_snapshot.repo_context.symbols),
            ("helper_value",),
        )
        self.assertIn("impact warning", failure_snapshot.warnings)
        self.assertIn("context warning", failure_snapshot.warnings)
        self.assertFalse(any(failure.stage == "command" for failure in outcome.session.failure_history))

    async def test_auto_verification_failure_records_state_and_enters_repair_loop(self):
        # Verifies that automatic post-patch verification failures are recorded structurally and feed the existing repair loop.
        # This catches a gap where post-patch py_compile failures would be lost or would stop without creating repairable session state.
        # The repair sequence is correct because the failed verification should produce a retryable command failure, trigger a repair patch, then rerun verification.
        log: list[str] = []
        workspace = Workspace(root_path="/tmp/agent-core-runner-auto-verify")
        run_id = new_run_id()
        repo_intelligence = _RecordingRepoIntelligence(
            refreshed_context=RepoContextResult(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                repo_map="refreshed repo map",
                file_summaries=(
                    FileSummary(path="app.py", summary="syntax fix target", content="def ok():\n    return 1\n"),
                ),
                warnings=("context warning",),
            ),
            impact_analysis=ImpactAnalysis(
                changed_paths=("app.py",),
                impacted_paths=("app.py",),
                warnings=("impact warning",),
            ),
            symbol_matches=(SymbolMatch(name="ok", kind="function", path="app.py", line=1),),
            log=log,
        )
        patch_diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch app.py",
                    step_id="step_1",
                    action_id="action_1_propose_patch_step_1",
                    target_files=("app.py",),
                    patch_diff=patch_diff,
                ),
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Repair syntax in app.py",
                    step_id="step_2",
                    action_id="action_2_propose_patch_step_2",
                    target_files=("app.py",),
                    patch_diff=patch_diff,
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            planned_verification=[
                AgentVerification(
                    command_argv=("python", "-m", "py_compile", "app.py"),
                    changed_files=("app.py",),
                ),
                AgentVerification(
                    command_argv=("python", "-m", "py_compile", "app.py"),
                    changed_files=("app.py",),
                ),
            ],
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("app.py",),
                patch_diff=patch_diff,
            ),
        )
        runtime = _RecordingFakeRuntime(
            command_results=[
                CommandResult(
                    run_id=run_id,
                    task_id="task_verify_fail",
                    exit_code=1,
                    stderr="SyntaxError: invalid syntax",
                ),
                CommandResult(
                    run_id=run_id,
                    task_id="task_verify_pass",
                    exit_code=0,
                    stdout="",
                ),
            ],
            log=log,
        )
        session_store = _RecordingSessionStore(log=log)
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
            repo_intelligence=repo_intelligence,
            repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
        )
        session = LocalAgentCoreService().create_session(
            run_id=run_id,
            workspace_id=workspace.workspace_id,
            user_request="Patch app.py and keep it syntactically valid",
            repo_context=RepoContextResult(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                repo_map="stale repo map",
                file_summaries=(FileSummary(path="app.py", summary="before patch", content="old\n"),),
            ),
            current_plan=AgentPlan(
                goal="Patch and verify",
                steps=[
                    AgentStep(step_id="step_1", kind="patch", description="Patch app.py", target_files=("app.py",)),
                    AgentStep(step_id="step_2", kind="patch", description="Repair syntax in app.py", target_files=("app.py",)),
                    AgentStep(step_id="step_3", kind="complete", description="Finish"),
                ],
            ),
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(
            runtime.calls,
            ["apply_patch", "execute_command", "apply_patch", "execute_command", "finalize_run"],
        )
        self.assertEqual(
            outcome.session.verification_history[-1].command_argv,
            ("python", "-m", "py_compile", "app.py"),
        )
        self.assertEqual(outcome.session.verification_history[-1].exit_code, 0)
        self.assertFalse(any(failure.stage == "command" for failure in outcome.session.failure_history))

        failure_snapshot = next(
            snapshot
            for snapshot in session_store.saved_sessions
            if snapshot.verification_history and snapshot.verification_history[-1].exit_code == 1
        )
        self.assertEqual(
            failure_snapshot.verification_history[-1].stderr,
            "SyntaxError: invalid syntax",
        )
        self.assertEqual(
            failure_snapshot.verification_history[-1].changed_files,
            ("app.py",),
        )
        self.assertTrue(failure_snapshot.verification_history[-1].failure_signature)
        self.assertEqual(failure_snapshot.failure_history[-1].stage, "command")
        self.assertTrue(failure_snapshot.failure_history[-1].retryable)
        self.assertEqual(
            failure_snapshot.failure_history[-1].details["stderr"],
            "SyntaxError: invalid syntax",
        )

    async def test_targeted_test_failure_reuses_repair_loop(self):
        # Verifies that a focused pytest failure after passing syntax verification reuses the same repair loop.
        # This catches a split-brain verification flow where targeted tests would fail outside the structured retryable command path.
        # The repair cycle is correct because the agent should fix the changed source file, rerun syntax verification, then rerun the same focused test.
        workspace = Workspace(root_path="/tmp/agent-core-runner-targeted-test")
        run_id = new_run_id()
        patch_diff = "--- a/services/agent_core/runner.py\n+++ b/services/agent_core/runner.py\n@@ -1 +1 @@\n-old\n+new\n"
        repo_intelligence = _RecordingRepoIntelligence(
            refreshed_context=RepoContextResult(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                repo_map="refreshed repo map",
                file_summaries=(
                    FileSummary(path="services/agent_core/runner.py", summary="repair target", content="def ok():\n    return 1\n"),
                    FileSummary(path="tests/unit/test_agent_core_runner.py", summary="focused test"),
                ),
            ),
            impact_analysis=ImpactAnalysis(
                changed_paths=("services/agent_core/runner.py",),
                impacted_paths=("services/agent_core/runner.py", "tests/unit/test_agent_core_runner.py"),
            ),
            log=[],
        )
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch runner.py",
                    step_id="step_1",
                    action_id="action_1_propose_patch_step_1",
                    target_files=("services/agent_core/runner.py",),
                    patch_diff=patch_diff,
                ),
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Repair runner.py",
                    step_id="step_2",
                    action_id="action_2_propose_patch_step_2",
                    target_files=("services/agent_core/runner.py",),
                    patch_diff=patch_diff,
                ),
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            planned_verification=[
                (
                    AgentVerification(
                        verification_level="syntax_only",
                        command_argv=("python", "-m", "py_compile", "services/agent_core/runner.py"),
                        changed_files=("services/agent_core/runner.py",),
                    ),
                    AgentVerification(
                        kind="targeted_pytest",
                        verification_level="targeted_tests_passed",
                        command_argv=("python", "-m", "pytest", "tests/unit/test_agent_core_runner.py"),
                        changed_files=("services/agent_core/runner.py",),
                    ),
                ),
                (
                    AgentVerification(
                        verification_level="syntax_only",
                        command_argv=("python", "-m", "py_compile", "services/agent_core/runner.py"),
                        changed_files=("services/agent_core/runner.py",),
                    ),
                    AgentVerification(
                        kind="targeted_pytest",
                        verification_level="targeted_tests_passed",
                        command_argv=("python", "-m", "pytest", "tests/unit/test_agent_core_runner.py"),
                        changed_files=("services/agent_core/runner.py",),
                    ),
                ),
            ],
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("services/agent_core/runner.py",),
                patch_diff=patch_diff,
            ),
        )
        runtime = _RecordingFakeRuntime(
            command_results=[
                CommandResult(run_id=run_id, task_id="task_syntax_1", exit_code=0),
                CommandResult(run_id=run_id, task_id="task_test_1", exit_code=1, stderr="AssertionError: expected True"),
                CommandResult(run_id=run_id, task_id="task_syntax_2", exit_code=0),
                CommandResult(run_id=run_id, task_id="task_test_2", exit_code=0, stdout="1 passed"),
            ],
        )
        session_store = _RecordingSessionStore()
        coordinator = AgentCoreCoordinator(
            agent_core=agent_core,
            execution_runtime=runtime,
            session_store=session_store,
            repo_intelligence=repo_intelligence,
            repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
        )
        session = LocalAgentCoreService().create_session(
            run_id=run_id,
            workspace_id=workspace.workspace_id,
            user_request="Patch runner.py and keep focused tests passing",
            repo_context=RepoContextResult(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                repo_map="stale repo map",
                file_summaries=(FileSummary(path="services/agent_core/runner.py", summary="before patch", content="old\n"),),
            ),
            current_plan=AgentPlan(
                goal="Patch and verify",
                steps=[
                    AgentStep(step_id="step_1", kind="patch", description="Patch runner.py", target_files=("services/agent_core/runner.py",)),
                    AgentStep(step_id="step_2", kind="patch", description="Repair runner.py", target_files=("services/agent_core/runner.py",)),
                    AgentStep(step_id="step_3", kind="complete", description="Finish"),
                ],
            ),
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(
            runtime.calls,
            ["apply_patch", "execute_command", "execute_command", "apply_patch", "execute_command", "execute_command", "finalize_run"],
        )
        self.assertEqual(
            [request.argv for request in runtime.command_requests],
            [
                ("python", "-m", "py_compile", "services/agent_core/runner.py"),
                ("python", "-m", "pytest", "tests/unit/test_agent_core_runner.py"),
                ("python", "-m", "py_compile", "services/agent_core/runner.py"),
                ("python", "-m", "pytest", "tests/unit/test_agent_core_runner.py"),
            ],
        )
        failed_verification = next(
            item for item in outcome.session.verification_history if item.exit_code == 1
        )
        self.assertEqual(failed_verification.verification_level, "targeted_tests_failed")
        self.assertFalse(any(failure.stage == "command" for failure in outcome.session.failure_history))

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

    async def test_coordinator_regenerates_patch_after_read_only_review_failure(self):
        patch_intent = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch app.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
        )
        valid_patch = replace(
            patch_intent,
            patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        agent_core = _SequencedFakeAgentCore(
            actions=[
                patch_intent,
                AgentAction(type=AgentActionType.COMPLETE, reason="done", summary_text="done"),
            ],
            generated_patch=[valid_patch, valid_patch],
            review=[
                AgentPatchReviewError(
                    "read_only_reference_modified",
                    "Patch modifies read-only reference files: ['docs/spec.md']",
                ),
                PatchReview(
                    accepted=True,
                    reason="Patch passed review",
                    changed_files=("app.py",),
                    patch_diff=valid_patch.patch_diff,
                ),
            ],
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "completed", outcome.session.failure_history)
        self.assertEqual(runtime.calls.count("apply_patch"), 1)
        self.assertEqual(outcome.session.failure_history[0].code, "read_only_reference_modified")
        self.assertTrue(outcome.session.failure_history[0].retryable)

    async def test_coordinator_fails_loudly_when_patch_generation_is_invalid(self):
        # Verifies that patch-generation failures stop execution before review/apply and surface a dedicated failure stage.
        # This catches permissive fallback behavior that would continue with an empty or malformed patch proposal.
        # The generate_patch failure is correct because no valid patch_diff exists yet, so the patch chain cannot continue safely.
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.PROPOSE_PATCH,
                    reason="Patch app.py",
                    target_files=("app.py",),
                )
            ],
            generated_patch=ValueError("malformed patch json"),
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.session.failure_history[-1].stage, "generate_patch")
        self.assertEqual(runtime.calls, [])

    async def test_coordinator_retries_retryable_patch_generation_failure_once_then_applies_patch(self):
        # Verifies that retryable patch-generation failures are recorded and retried before any runtime side effect.
        # This catches a brittle patch loop that would fail immediately on one recoverable model miss instead of retrying safely.
        # Completion is correct because the first failure is retryable, the second generated patch is valid, and apply_patch runs once.
        log: list[str] = []
        patch_action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch app.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
        )
        generated_patch = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch app.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
            patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-before\n+after\n",
        )
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                patch_action,
                AgentAction(
                    type=AgentActionType.COMPLETE,
                    reason="Run complete",
                    summary_text="Everything succeeded",
                ),
            ],
            generated_patch=[
                AgentPatchGenerationError("search_not_found", "SEARCH block did not match"),
                generated_patch,
            ],
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("app.py",),
                patch_diff=generated_patch.patch_diff,
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
                AgentStep(
                    step_id="step_1",
                    kind="patch",
                    description="Patch app.py",
                    target_files=("app.py",),
                ),
                AgentStep(
                    step_id="step_2",
                    kind="complete",
                    description="Finish",
                ),
            ],
            repo_context=RepoContextResult(
                workspace_id=new_workspace_id(),
                run_id=new_run_id(),
                repo_map="app.py\n",
                file_summaries=(
                    FileSummary(path="app.py", summary="text file", content="before\n"),
                ),
            ),
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(log.count("generate_patch"), 2)
        self.assertEqual(runtime.calls.count("apply_patch"), 1)
        self.assertEqual(outcome.session.failure_history[0].code, "search_not_found")
        self.assertTrue(outcome.session.failure_history[0].retryable)

    async def test_coordinator_stops_when_the_same_retryable_patch_failure_repeats(self):
        # Verifies that repeating the same retryable failure does not loop indefinitely.
        # This catches recovery logic that keeps asking the model for the same broken patch output over and over.
        # Failure is correct because the same search_not_found error happened twice, so the coordinator stops safely.
        patch_action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch app.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
        )
        agent_core = _SequencedFakeAgentCore(
            actions=[patch_action],
            generated_patch=[
                AgentPatchGenerationError("search_not_found", "SEARCH block did not match"),
                AgentPatchGenerationError("search_not_found", "SEARCH block did not match"),
            ],
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)
        session = self._make_session_with_plan(
            steps=[
                AgentStep(
                    step_id="step_1",
                    kind="patch",
                    description="Patch app.py",
                    target_files=("app.py",),
                )
            ],
            repo_context=RepoContextResult(
                workspace_id=new_workspace_id(),
                run_id=new_run_id(),
                repo_map="app.py\n",
                file_summaries=(
                    FileSummary(path="app.py", summary="text file", content="before\n"),
                ),
            ),
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(len(outcome.session.failure_history), 2)
        self.assertEqual(outcome.session.failure_history[0].code, "search_not_found")
        self.assertTrue(outcome.session.failure_history[0].retryable)
        self.assertEqual(outcome.session.failure_history[1].stage, "generate_patch")
        self.assertEqual(runtime.calls, [])

    async def test_coordinator_retries_post_apply_validation_failure_before_runtime_apply(self):
        # Verifies that invalid post-apply Python syntax is caught before runtime patch application and can trigger one retry.
        # This catches a dangerous path where malformed generated code would be applied to the workspace before basic validation.
        # Completion is correct because the first patch makes invalid Python, the retry fixes it, and only the valid diff reaches apply_patch.
        log: list[str] = []
        patch_action = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch app.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
        )
        invalid_patch = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch app.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
            patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-def add():\n-    return 1\n+def add(:\n+    return 1\n",
        )
        valid_patch = AgentAction(
            type=AgentActionType.PROPOSE_PATCH,
            reason="Patch app.py",
            step_id="step_1",
            action_id="action_1_propose_patch_step_1",
            target_files=("app.py",),
            patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def add():\n-    return 1\n+    return 2\n",
        )
        agent_core = _SequencedFakeAgentCore(
            log=log,
            actions=[
                patch_action,
                AgentAction(type=AgentActionType.COMPLETE, reason="done", summary_text="done"),
            ],
            generated_patch=[invalid_patch, valid_patch],
            review=PatchReview(
                accepted=True,
                reason="Patch passed review",
                changed_files=("app.py",),
            ),
        )
        runtime = _RecordingFakeRuntime(log=log)
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)
        session = self._make_session_with_plan(
            steps=[
                AgentStep(
                    step_id="step_1",
                    kind="patch",
                    description="Patch app.py",
                    target_files=("app.py",),
                ),
                AgentStep(step_id="step_2", kind="complete", description="Finish"),
            ],
            repo_context=RepoContextResult(
                workspace_id=new_workspace_id(),
                run_id=new_run_id(),
                repo_map="app.py\n",
                file_summaries=(
                    FileSummary(
                        path="app.py",
                        summary="python file",
                        language="python",
                        content="def add():\n    return 1\n",
                    ),
                ),
            ),
        )

        outcome = await coordinator.run(session)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(log.count("generate_patch"), 2)
        self.assertEqual(runtime.calls.count("apply_patch"), 1)
        self.assertEqual(outcome.session.failure_history[0].code, "post_apply_validation_failed")
        self.assertTrue(outcome.session.failure_history[0].retryable)

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

    async def test_coordinator_fails_loudly_when_command_generation_is_invalid(self):
        # Verifies that command-generation failures stop execution before runtime dispatch and surface a dedicated failure stage.
        # This catches permissive fallback behavior that would continue with an empty or malformed command payload.
        # The generate_command failure is correct because no valid command_argv exists yet, so runtime execution cannot begin safely.
        agent_core = _SequencedFakeAgentCore(
            actions=[
                AgentAction(
                    type=AgentActionType.RUN_COMMAND,
                    reason="Run targeted tests",
                    target_files=("tests/unit/test_agent_core_runner.py",),
                )
            ],
            generated_command=ValueError("malformed command json"),
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(agent_core=agent_core, execution_runtime=runtime)

        outcome = await coordinator.run(self._make_session())

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.session.failure_history[-1].stage, "generate_command")
        self.assertEqual(runtime.calls, [])

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

    async def test_resume_after_denied_approval_finalizes_failed_run(self):
        pending_action = AgentAction(
            type=AgentActionType.RUN_COMMAND,
            reason="Run risky command",
            step_id="step_1",
            action_id="action_1_run_command_step_1",
            command_argv=("git", "push"),
        )
        stored_session = self._make_session_with_plan(
            steps=[AgentStep(step_id="step_1", kind="command", description="Run risky command")],
            action_history=[pending_action],
            pending_action=pending_action,
            pending_approval_id="approval_123",
            iteration_count=1,
        )
        runtime = _RecordingFakeRuntime()
        coordinator = AgentCoreCoordinator(
            agent_core=_SequencedFakeAgentCore(actions=[]),
            execution_runtime=runtime,
            session_store=_RecordingSessionStore(stored_session),
        )

        outcome = await coordinator.resume_after_approval(
            str(stored_session.run_id),
            approved=False,
            reviewer="human",
            comment="too risky",
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(runtime.finalized_status, RunStatus.FAILED)
        self.assertEqual(runtime.finalized_summary, None)
        self.assertEqual(runtime.calls, ["record_approval_decision", "finalize_run"])

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
