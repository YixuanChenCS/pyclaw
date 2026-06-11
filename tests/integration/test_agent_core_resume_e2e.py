from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from packages.shared_types import (
    ApprovalRequest,
    CommandRequest,
    EventType,
    PatchProposal,
    RepoContextResult,
    RunRequest,
    Session,
    Workspace,
)
from packages.shared_types.ids import new_run_id
from services.agent_core import (
    AgentAction,
    AgentActionType,
    AgentCoreCoordinator,
    AgentSessionPhase,
    FakeModelClient,
    LocalAgentCoreService,
)
from services.agent_core.reducer import record_selected_action
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class _InMemoryRepoStore:
    def __init__(self, workspaces: dict[str, Workspace]) -> None:
        self._workspaces = dict(workspaces)

    async def get_workspace(self, workspace_id):
        return self._workspaces.get(str(workspace_id))


class TestAgentCoreResumeE2E(unittest.IsolatedAsyncioTestCase):
    def _make_runtime(
        self,
        root: Path,
        *,
        workspace: Workspace,
    ) -> tuple[LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository]:
        repository = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
        runtime = LocalExecutionRuntimeService(
            repository=repository,
            repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
            stream_poll_interval=0.01,
        )
        return runtime, repository

    def _make_run_request(self, *, workspace: Workspace, run_id) -> RunRequest:
        session = Session(workspace_id=workspace.workspace_id, title="agent-core-resume")
        return RunRequest(
            run_id=run_id,
            workspace_id=workspace.workspace_id,
            session_id=session.session_id,
            prompt="Resume deterministic agent_core work",
        )

    def _make_agent_core(self, *, responses, session_store) -> LocalAgentCoreService:
        return LocalAgentCoreService(
            model_client=FakeModelClient(responses=list(responses)),
            session_store=session_store,
        )

    def _make_repo_context(self, *, workspace: Workspace, run_id) -> RepoContextResult:
        return RepoContextResult(
            workspace_id=workspace.workspace_id,
            run_id=run_id,
            repo_map="services/\n  agent_core/\n  execution_runtime/\n",
        )

    async def test_resume_after_process_restart_replays_completed_command_once(self):
        # Verifies that after planning and persisting a selected command action, a rebuilt coordinator/runtime reuses the finished runtime task.
        # This catches duplicate command execution after restart when the task already completed with the same action_id/task_id.
        # Replay is correct because command.started/completed already exist for the persisted pending action and resume should only advance state.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            run_id = new_run_id()
            runtime, repository = self._make_runtime(root, workspace=workspace)
            await runtime.enqueue_run(self._make_run_request(workspace=workspace, run_id=run_id))
            await runtime.claim_next_run("worker-a", lease_seconds=30)

            agent_core = self._make_agent_core(
                responses=[
                    {
                        "goal": "Run a deterministic command and finish",
                        "steps": [
                            {
                                "kind": "command",
                                "description": "Run verification",
                                "rationale": "command: sh -c 'printf resumed-command'",
                            },
                            {
                                "kind": "complete",
                                "description": "Finish the run",
                            },
                        ],
                    }
                ],
                session_store=repository,
            )
            session = agent_core.create_session(
                run_id=run_id,
                workspace_id=workspace.workspace_id,
                user_request="Resume command work safely",
                repo_context=self._make_repo_context(workspace=workspace, run_id=run_id),
            )

            plan = await agent_core.create_plan(session)
            planned_session = replace(session, current_plan=plan, phase=AgentSessionPhase.READY)
            action = await agent_core.next_action(planned_session)
            pending_session, selected_action = record_selected_action(planned_session, action)
            await repository.save_agent_session(pending_session)
            await runtime.execute_command(
                CommandRequest(
                    run_id=run_id,
                    task_id=selected_action.action_id,
                    argv=selected_action.command_argv,
                )
            )

            rebuilt_runtime = LocalExecutionRuntimeService(
                repository=repository,
                repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
                stream_poll_interval=0.01,
            )
            rebuilt_agent_core = self._make_agent_core(responses=[], session_store=repository)
            coordinator = AgentCoreCoordinator(
                agent_core=rebuilt_agent_core,
                execution_runtime=rebuilt_runtime,
                session_store=repository,
            )

            outcome = await coordinator.resume(str(run_id))
            events = await repository.list_events(run_id)

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(outcome.session.phase, AgentSessionPhase.COMPLETED)
            self.assertEqual(
                [event.event_type for event in events].count(EventType.COMMAND_STARTED),
                1,
            )
            self.assertEqual(
                [event.event_type for event in events].count(EventType.COMMAND_COMPLETED),
                1,
            )
            self.assertEqual(
                [event.event_type for event in events].count(EventType.RUN_COMPLETED),
                1,
            )

    async def test_resume_after_process_restart_replays_completed_patch_once(self):
        # Verifies that after planning and persisting a selected patch action, a rebuilt coordinator/runtime reuses the finished patch artifact.
        # This catches duplicate patch side effects after restart when the patch already applied for the same action_id/task_id.
        # Replay is correct because patch.started/patch.applied already exist for the persisted pending action and resume should only advance state.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            run_id = new_run_id()
            runtime, repository = self._make_runtime(root, workspace=workspace)
            await runtime.enqueue_run(self._make_run_request(workspace=workspace, run_id=run_id))
            await runtime.claim_next_run("worker-a", lease_seconds=30)

            file_path = root / "app.txt"
            file_path.write_text("before\n", encoding="utf-8")
            patch_diff = "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-before\n+after\n"

            agent_core = self._make_agent_core(
                responses=[
                    {
                        "goal": "Patch a file and finish",
                        "steps": [
                            {
                                "kind": "patch",
                                "description": "Patch app.txt",
                                "target_files": ["app.txt"],
                            },
                            {
                                "kind": "complete",
                                "description": "Finish the run",
                            },
                        ],
                    }
                ],
                session_store=repository,
            )
            session = agent_core.create_session(
                run_id=run_id,
                workspace_id=workspace.workspace_id,
                user_request="Resume patch work safely",
                repo_context=self._make_repo_context(workspace=workspace, run_id=run_id),
            )

            plan = await agent_core.create_plan(session)
            planned_session = replace(session, current_plan=plan, phase=AgentSessionPhase.READY)
            patch_action = AgentAction(
                type=AgentActionType.PROPOSE_PATCH,
                reason="Patch app.txt",
                step_id=plan.steps[0].step_id,
                action_id="action_1_propose_patch_step_1",
                target_files=("app.txt",),
                patch_diff=patch_diff,
            )
            pending_session, selected_action = record_selected_action(planned_session, patch_action)
            await repository.save_agent_session(pending_session)
            await runtime.apply_patch(
                str(run_id),
                PatchProposal(
                    run_id=run_id,
                    task_id=selected_action.action_id,
                    summary=selected_action.reason,
                    unified_diff=patch_diff,
                    target_paths=selected_action.target_files,
                ),
            )

            rebuilt_runtime = LocalExecutionRuntimeService(
                repository=repository,
                repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
                stream_poll_interval=0.01,
            )
            rebuilt_agent_core = self._make_agent_core(responses=[], session_store=repository)
            coordinator = AgentCoreCoordinator(
                agent_core=rebuilt_agent_core,
                execution_runtime=rebuilt_runtime,
                session_store=repository,
            )

            outcome = await coordinator.resume(str(run_id))
            events = await repository.list_events(run_id)
            artifacts = await repository.list_artifacts(run_id)

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(outcome.session.phase, AgentSessionPhase.COMPLETED)
            self.assertEqual(file_path.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(
                [event.event_type for event in events].count(EventType.PATCH_APPLIED),
                1,
            )
            self.assertEqual(
                [
                    event
                    for event in events
                    if event.event_type == EventType.AGENT_MESSAGE and event.payload.get("kind") == "patch.started"
                ].__len__(),
                1,
            )

    async def test_resume_after_process_restart_does_not_duplicate_approval_and_then_can_continue(self):
        # Verifies that after planning and persisting a selected approval action, a rebuilt coordinator/runtime replays the existing approval checkpoint.
        # This catches duplicate approval rows/events after restart when the same action_id/task_id has already suspended the run once.
        # One approval side effect is correct because the persisted pending approval is already the canonical checkpoint for that action.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            run_id = new_run_id()
            runtime, repository = self._make_runtime(root, workspace=workspace)
            await runtime.enqueue_run(self._make_run_request(workspace=workspace, run_id=run_id))
            await runtime.claim_next_run("worker-a", lease_seconds=30)

            agent_core = self._make_agent_core(
                responses=[
                    {
                        "goal": "Request approval and then finish",
                        "steps": [
                            {
                                "kind": "approval",
                                "description": "Need human approval",
                            },
                            {
                                "kind": "complete",
                                "description": "Finish the run",
                            },
                        ],
                    }
                ],
                session_store=repository,
            )
            session = agent_core.create_session(
                run_id=run_id,
                workspace_id=workspace.workspace_id,
                user_request="Resume approval work safely",
                repo_context=self._make_repo_context(workspace=workspace, run_id=run_id),
            )

            plan = await agent_core.create_plan(session)
            planned_session = replace(session, current_plan=plan, phase=AgentSessionPhase.READY)
            action = await agent_core.next_action(planned_session)
            pending_session, selected_action = record_selected_action(planned_session, action)
            await repository.save_agent_session(pending_session)
            approval_id = await runtime.request_approval(
                str(run_id),
                ApprovalRequest(
                    run_id=run_id,
                    task_id=selected_action.action_id,
                    reason=selected_action.approval_message or selected_action.reason,
                    command_argv=selected_action.command_argv,
                ),
            )
            await repository.save_agent_session(
                replace(
                    pending_session,
                    phase=AgentSessionPhase.AWAITING_APPROVAL,
                    pending_approval_id=approval_id,
                )
            )

            rebuilt_runtime = LocalExecutionRuntimeService(
                repository=repository,
                repo_store=_InMemoryRepoStore({str(workspace.workspace_id): workspace}),
                stream_poll_interval=0.01,
            )
            rebuilt_agent_core = self._make_agent_core(responses=[], session_store=repository)
            coordinator = AgentCoreCoordinator(
                agent_core=rebuilt_agent_core,
                execution_runtime=rebuilt_runtime,
                session_store=repository,
            )

            suspended_outcome = await coordinator.resume(str(run_id))
            approvals_after_resume = await repository.list_approval_requests(run_id)
            continued_outcome = await coordinator.resume_after_approval(
                str(run_id),
                approved=True,
                reviewer="human",
                comment="looks safe",
            )
            events = await repository.list_events(run_id)

            self.assertEqual(suspended_outcome.status, "approval_requested")
            self.assertEqual(suspended_outcome.session.phase, AgentSessionPhase.AWAITING_APPROVAL)
            self.assertEqual(len(approvals_after_resume), 1)
            self.assertEqual(
                [event.event_type for event in events].count(EventType.APPROVAL_REQUESTED),
                1,
            )
            self.assertEqual(continued_outcome.status, "completed")
            self.assertEqual(continued_outcome.session.phase, AgentSessionPhase.COMPLETED)
