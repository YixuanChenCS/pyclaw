from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apps.api import (
    create_local_platform_api_from_config,
    create_local_platform_api_from_env,
    create_platform_api,
)
from apps._local_support import NoopObservabilityService, WorkspaceRegistryRepoStore
from apps.cli.app import _LocalCLIApplication, build_cli_parser
from apps.cli import (
    create_cli_application,
    create_local_cli_application_from_config,
    create_local_cli_application_from_env,
    resolve_local_cli_runner_config,
)
from packages.shared_types import (
    ApprovalDecision,
    FileSummary,
    RepoContextResult,
    RunRequest,
    RunStatus,
    Session,
    Workspace,
    new_run_id,
)
from services.agent_core import AgentAction, AgentActionType, AgentCoreCoordinator, AgentCoreModelConfig, LocalAgentRunnerConfig
from services.agent_core import FakeModelClient, LocalAgentCoreService
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class _NoopRuntime:
    async def enqueue_run(self, request):
        return str(request.run_id or "run_test")

    async def stream_events(self, run_id):
        if False:
            yield run_id


class _RepoIntelligence:
    async def inspect_workspace(self, workspace):
        return workspace

    async def build_context(self, request):
        return RepoContextResult(
            workspace_id=request.workspace_id,
            run_id=request.run_id,
            repo_map="services/agent_core/model_client.py",
        )


class _Observability:
    async def get_health(self):
        from packages.shared_types import HealthCheckResult

        return HealthCheckResult(service="test-observability", status="ready")


class _RepoStore:
    def __init__(self, workspace):
        self._workspace = workspace

    async def get_workspace(self, workspace_id):
        if str(workspace_id) == str(self._workspace.workspace_id):
            return self._workspace
        return None


class _ApprovalCoordinator:
    def __init__(self) -> None:
        self.calls = []

    async def resume_after_approval(self, run_id, *, approved, reviewer=None, comment=None):
        self.calls.append((run_id, approved, reviewer, comment))


class _PatchRepoIntelligence:
    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self._workspace_root = Path(workspace.root_path)
        self.context_requests = []

    async def inspect_workspace(self, workspace):
        return self._workspace

    async def build_context(self, request):
        self.context_requests.append(request)
        return RepoContextResult(
            workspace_id=request.workspace_id,
            run_id=request.run_id,
            file_summaries=tuple(
                FileSummary(
                    path=target_path,
                    content=(self._workspace_root / target_path).read_text(encoding="utf-8"),
                )
                for target_path in request.target_paths
            ),
            reference_file_summaries=tuple(
                FileSummary(
                    path=reference_path,
                    content=(self._workspace_root / reference_path).read_text(encoding="utf-8"),
                )
                for reference_path in request.reference_paths
            ),
            repo_map="\n".join(request.target_paths),
        )


class TestAppEntrypoints(unittest.IsolatedAsyncioTestCase):
    async def test_cli_agent_plan_command_runs_create_plan_through_real_service(self):
        # Verifies that the CLI entrypoint parses a plan command and routes it through agent_core.create_plan.
        # This catches the integration gap where bootstrap existed but no command-line path actually invoked the planning service.
        # The printed plan is correct because the fake model returns one valid inspect step and the CLI should serialize that plan to JSON.
        agent_core = LocalAgentCoreService(
            model_client=FakeModelClient(
                responses=[
                    {
                        "goal": "Inspect the model client",
                        "steps": [
                            {
                                "kind": "inspect",
                                "description": "Inspect services/agent_core/model_client.py",
                                "target_files": ["services/agent_core/model_client.py"],
                            }
                        ],
                    }
                ]
            )
        )
        app = create_cli_application(
            agent_core=agent_core,
            execution_runtime=_NoopRuntime(),
            repo_intelligence=_RepoIntelligence(),
            observability=_Observability(),
        )

        with patch("builtins.print") as mocked_print:
            exit_code = await app.run(
                [
                    "agent-plan",
                    "--workspace",
                    ".",
                    "--prompt",
                    "Plan the work",
                    "--target-path",
                    "services/agent_core/model_client.py",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(mocked_print.call_count, 1)
        rendered = mocked_print.call_args.args[0]
        self.assertIn('"goal": "Inspect the model client"', rendered)
        self.assertIn('"kind": "inspect"', rendered)

    async def test_platform_api_list_runs_reads_back_local_runtime_runs(self):
        # Verifies that the API entrypoint is wired to the local SQLite runtime store rather than returning placeholder data.
        # This catches a fake-control-plane bug where the API layer imports successfully but cannot actually observe queued runs.
        # The queued run result is correct because enqueue_run persists one run row and list_runs should surface that exact record.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            session = Session(workspace_id=workspace.workspace_id, title="api-entrypoint")
            repository = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
            runtime = LocalExecutionRuntimeService(
                repository=repository,
                repo_store=_RepoStore(workspace),
                stream_poll_interval=0.01,
            )
            run_id = new_run_id()
            await runtime.enqueue_run(
                RunRequest(
                    run_id=run_id,
                    workspace_id=workspace.workspace_id,
                    session_id=session.session_id,
                    prompt="Queue one run",
                )
            )

            api = create_platform_api(
                agent_core=LocalAgentCoreService(),
                execution_runtime=runtime,
                repo_intelligence=_RepoIntelligence(),
                observability=_Observability(),
            )

            runs = await api.list_runs(str(workspace.workspace_id))
            loaded = await api.get_run(str(run_id))

        self.assertEqual(len(runs), 1)
        self.assertEqual(str(runs[0].run_id), str(run_id))
        self.assertEqual(runs[0].status.value, "queued")
        self.assertIsNotNone(loaded)
        self.assertEqual(str(loaded.run_id), str(run_id))
        self.assertEqual(loaded.status.value, "queued")

    async def test_platform_api_list_runs_filters_by_session_and_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            first_session = Session(workspace_id=workspace.workspace_id)
            second_session = Session(workspace_id=workspace.workspace_id)
            repository = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
            runtime = LocalExecutionRuntimeService(repository=repository, repo_store=_RepoStore(workspace))
            first_run_id = await runtime.enqueue_run(
                RunRequest(
                    workspace_id=workspace.workspace_id,
                    session_id=first_session.session_id,
                    prompt="First",
                )
            )
            await runtime.enqueue_run(
                RunRequest(
                    workspace_id=workspace.workspace_id,
                    session_id=second_session.session_id,
                    prompt="Second",
                )
            )
            await repository.update_run_status(first_run_id, RunStatus.CANCELLED)
            api = create_platform_api(
                agent_core=LocalAgentCoreService(),
                execution_runtime=runtime,
                repo_intelligence=_RepoIntelligence(),
                observability=_Observability(),
            )

            runs = await api.list_runs(
                str(workspace.workspace_id),
                session_id=str(first_session.session_id),
                status=RunStatus.CANCELLED,
            )

        self.assertEqual([str(run.run_id) for run in runs], [first_run_id])

    async def test_platform_api_stream_replays_after_last_event_uuid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            session = Session(workspace_id=workspace.workspace_id)
            repository = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
            runtime = LocalExecutionRuntimeService(
                repository=repository,
                repo_store=_RepoStore(workspace),
                stream_poll_interval=0.01,
            )
            run_id = await runtime.enqueue_run(
                RunRequest(
                    workspace_id=workspace.workspace_id,
                    session_id=session.session_id,
                    prompt="Stream",
                )
            )
            events = await repository.list_events(run_id)
            api = create_platform_api(
                agent_core=LocalAgentCoreService(),
                execution_runtime=runtime,
                repo_intelligence=_RepoIntelligence(),
                observability=_Observability(),
            )

            stream = api.stream_run_events(run_id, last_event_id=str(events[0].event_id))
            replayed = await asyncio.wait_for(anext(stream), timeout=1)
            await stream.aclose()

        self.assertEqual(replayed.sequence, 2)

    async def test_platform_api_submit_approval_resumes_through_coordinator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = Workspace(root_path=tmpdir)
            session = Session(workspace_id=workspace.workspace_id)
            repository = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
            runtime = LocalExecutionRuntimeService(repository=repository, repo_store=_RepoStore(workspace))
            run_id = await runtime.enqueue_run(
                RunRequest(
                    workspace_id=workspace.workspace_id,
                    session_id=session.session_id,
                    prompt="Approve",
                )
            )
            approval_id = "approval_test"
            agent_core = LocalAgentCoreService()
            await repository.save_agent_session(
                agent_core.create_session(
                    run_id=run_id,
                    workspace_id=workspace.workspace_id,
                    user_request="Approve",
                    pending_action=AgentAction(
                        type=AgentActionType.REQUEST_APPROVAL,
                        reason="Approve",
                    ),
                    pending_approval_id=approval_id,
                )
            )
            coordinator = _ApprovalCoordinator()
            api = create_platform_api(
                agent_core=agent_core,
                execution_runtime=runtime,
                repo_intelligence=_RepoIntelligence(),
                observability=_Observability(),
                coordinator=coordinator,
            )

            await api.submit_approval(
                ApprovalDecision(
                    approval_id=approval_id,
                    run_id=run_id,
                    approved=True,
                    reviewer="api-user",
                )
            )

        self.assertEqual(coordinator.calls, [(run_id, True, "api-user", None)])

    async def test_platform_api_health_aggregates_runtime_health(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(root_path=tmpdir)
            runtime = LocalExecutionRuntimeService(
                repository=SQLiteExecutionRuntimeRepository(Path(tmpdir) / "runtime.sqlite3"),
                repo_store=_RepoStore(workspace),
            )
            api = create_platform_api(
                agent_core=LocalAgentCoreService(),
                execution_runtime=runtime,
                repo_intelligence=_RepoIntelligence(),
                observability=_Observability(),
            )

            health = await api.get_health()

        self.assertEqual(health.status, "ready")
        self.assertEqual(health.details["runtime"]["details"]["db"], "ready")

    async def test_local_cli_factory_uses_bootstrap_stack(self):
        # Verifies that the local CLI factory goes through the env-based bootstrap path instead of manual ad-hoc service construction.
        # This catches regressions where AGENT_CORE_MODEL selection would be bypassed because the CLI created its own hardcoded services.
        # The factory result is correct because the patched bootstrap function supplies the exact services the CLI should expose.
        fake_stack = type(
            "FakeStack",
            (),
            {
                "agent_core": LocalAgentCoreService(),
                "execution_runtime": _NoopRuntime(),
                "coordinator": object(),
            },
        )()
        fake_config = LocalAgentRunnerConfig()

        with patch("apps.cli.app.resolve_local_agent_runner_config", return_value=fake_config):
            with patch("apps.cli.app.build_local_agent_runner_stack", return_value=fake_stack):
                app = create_local_cli_application_from_env()

        self.assertIsNotNone(app)

    async def test_local_cli_factory_passes_resolved_config_into_stack_builder(self):
        # Verifies that the env-based CLI factory feeds the resolved precedence config into the stack builder.
        # This catches the bug where config resolution exists but the CLI still instantiates a stack from raw environment state.
        # The builder call is correct because the resolved config object should be the sole source of model/provider/key selection at that point.
        fake_stack = type(
            "FakeStack",
            (),
            {
                "agent_core": LocalAgentCoreService(),
                "execution_runtime": _NoopRuntime(),
                "coordinator": object(),
            },
        )()
        fake_config = LocalAgentRunnerConfig()

        with patch("apps.cli.app.resolve_local_agent_runner_config", return_value=fake_config):
            with patch("apps.cli.app.build_local_agent_runner_stack", return_value=fake_stack) as mocked_build:
                app = create_local_cli_application_from_env()

        self.assertIsNotNone(app)
        self.assertIs(mocked_build.call_args.kwargs["config"], fake_config)

    async def test_create_local_cli_application_from_config_uses_explicit_config_stack(self):
        # Verifies that the CLI can be assembled from an explicit resolved config, not only from raw environment variables.
        # This catches the integration gap where precedence resolution might work but there is no way to pass the resolved config into the CLI bootstrap.
        # The factory result is correct because the patched stack builder should be called with the exact explicit config object.
        fake_stack = type(
            "FakeStack",
            (),
            {
                "agent_core": LocalAgentCoreService(),
                "execution_runtime": _NoopRuntime(),
                "coordinator": object(),
            },
        )()
        config = LocalAgentRunnerConfig(
            model=AgentCoreModelConfig(model="openai/gpt-4.1-mini")
        )

        with patch("apps.cli.app.build_local_agent_runner_stack", return_value=fake_stack) as mocked_build:
            app = create_local_cli_application_from_config(config)

        self.assertIsNotNone(app)
        mocked_build.assert_called_once()
        self.assertIs(mocked_build.call_args.kwargs["config"], config)

    async def test_local_platform_factory_uses_bootstrap_stack(self):
        # Verifies that the local API factory also goes through the env-based bootstrap path.
        # This catches the wiring bug where the CLI and API would diverge and only one of them honored configured model bootstrap.
        # The factory result is correct because the patched bootstrap stack is the single source of runtime/agent_core assembly for local mode.
        fake_stack = type(
            "FakeStack",
            (),
            {
                "agent_core": LocalAgentCoreService(),
                "execution_runtime": _NoopRuntime(),
                "coordinator": object(),
            },
        )()
        fake_config = LocalAgentRunnerConfig()

        with patch("apps.api.app.resolve_local_agent_runner_config", return_value=fake_config):
            with patch("apps.api.app.build_local_agent_runner_stack", return_value=fake_stack):
                api = create_local_platform_api_from_env()

        self.assertIsNotNone(api)

    async def test_local_platform_factory_passes_explicit_config_into_stack_builder(self):
        # Verifies that the config-based API factory uses the already-resolved config rather than re-reading environment state.
        # This catches precedence drift where API bootstrap would ignore caller-supplied config and silently rebuild from env.
        # The builder call is correct because the explicit config object should remain authoritative once resolution is complete.
        fake_stack = type(
            "FakeStack",
            (),
            {
                "agent_core": LocalAgentCoreService(),
                "execution_runtime": _NoopRuntime(),
                "coordinator": object(),
            },
        )()
        config = LocalAgentRunnerConfig()

        with patch("apps.api.app.build_local_agent_runner_stack", return_value=fake_stack) as mocked_build:
            api = create_local_platform_api_from_config(config)

        self.assertIsNotNone(api)
        self.assertIs(mocked_build.call_args.kwargs["config"], config)

    async def test_local_platform_factory_passes_resolved_config_into_stack_builder(self):
        # Verifies that the env-based API factory also threads the resolved config into the stack builder.
        # This catches the bug where API bootstrap would resolve precedence correctly but then discard that result.
        # The builder call is correct because the resolved config should be the exact input to local stack construction.
        fake_stack = type(
            "FakeStack",
            (),
            {
                "agent_core": LocalAgentCoreService(),
                "execution_runtime": _NoopRuntime(),
                "coordinator": object(),
            },
        )()
        fake_config = LocalAgentRunnerConfig()

        with patch("apps.api.app.resolve_local_agent_runner_config", return_value=fake_config):
            with patch("apps.api.app.build_local_agent_runner_stack", return_value=fake_stack) as mocked_build:
                api = create_local_platform_api_from_env()

        self.assertIsNotNone(api)
        self.assertIs(mocked_build.call_args.kwargs["config"], fake_config)

    async def test_resolve_local_cli_runner_config_enforces_cli_precedence(self):
        # Verifies that the CLI-level resolver applies explicit flags over project config and environment variables.
        # This catches precedence bugs where parsing succeeds but the effective model/key still come from a lower-priority source.
        # The resolved values are correct because CLI flags are the highest-priority source in the intended configuration order.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".agent_core.yml").write_text(
                """
model:
  model: openai/gpt-4.1-mini
api_keys:
  openai: file-openai-key
""".strip(),
                encoding="utf-8",
            )

            config = resolve_local_cli_runner_config(
                [
                    "--model",
                    "openai/gpt-5-mini",
                    "--api-key",
                    "openai=cli-openai-key",
                    "agent-plan",
                    "--workspace",
                    str(root),
                    "--prompt",
                    "Plan the work",
                ],
                env={
                    "AGENT_CORE_MODEL": "openai/gpt-4o-mini",
                    "OPENAI_API_KEY": "env-openai-key",
                },
            )

        self.assertEqual(config.model.model, "openai/gpt-5-mini")
        self.assertEqual(dict(config.api_keys), {"openai": "cli-openai-key"})

    async def test_resolve_local_cli_runner_config_openai_flag_beats_generic_api_key(self):
        # Verifies that the provider-specific --openai-api-key flag overrides a generic --api-key openai=... entry on the same CLI.
        # This catches ambiguous CLI behavior where two explicit sources disagree and the more specific flag does not win deterministically.
        # The OpenAI key is correct because the dedicated provider flag is the most explicit representation of that credential on the command line.
        config = resolve_local_cli_runner_config(
            [
                "--api-key",
                "openai=generic-key",
                "--openai-api-key",
                "specific-key",
                "agent-plan",
                "--workspace",
                ".",
                "--prompt",
                "Plan the work",
            ]
        )

        self.assertEqual(dict(config.api_keys), {"openai": "specific-key"})

    async def test_resolve_local_cli_runner_config_parses_fallback_test_command(self):
        config = resolve_local_cli_runner_config(
            [
                "--fallback-test-command",
                "python -m pytest tests/unit -k agent_core",
                "agent-plan",
                "--workspace",
                ".",
                "--prompt",
                "Plan the work",
            ]
        )

        self.assertEqual(
            config.fallback_test_command,
            ("python", "-m", "pytest", "tests/unit", "-k", "agent_core"),
        )

    async def test_cli_parser_preserves_multiple_read_only_paths_for_agent_run(self):
        args = build_cli_parser().parse_args(
            [
                "agent-run",
                "--workspace",
                ".",
                "--prompt",
                "Run the agent",
                "--read-only",
                "docs/spec.md",
                "--read-only",
                "README.md",
            ]
        )

        self.assertEqual(args.read_only, ["docs/spec.md", "README.md"])
        self.assertEqual(args.target_path, [])

    async def test_cli_parser_defaults_read_only_paths_to_empty(self):
        args = build_cli_parser().parse_args(
            [
                "agent-patch",
                "--workspace",
                ".",
                "--prompt",
                "Patch app.txt",
                "--target-path",
                "app.txt",
            ]
        )

        self.assertEqual(args.read_only, [])

    async def test_cli_agent_patch_claims_the_new_run_before_applying_side_effects(self):
        # Verifies that agent-patch claims its own newly created run instead of an older queued run before patch application.
        # This catches the live lifecycle bug where apply_patch failed with "status queued" because claim_next_run activated the wrong run.
        # Completion is correct because the requested run is claimed, reviewed, patched, finalized, and the older queued run remains untouched.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "app.txt"
            target.write_text("before\n", encoding="utf-8")
            docs = root / "docs"
            docs.mkdir()
            reference = docs / "spec.md"
            reference.write_text("replace before with after\n", encoding="utf-8")
            readme = root / "README.md"
            readme.write_text("# reference\n", encoding="utf-8")

            workspace_store = WorkspaceRegistryRepoStore()
            workspace = Workspace(root_path=tmpdir)
            workspace_store.register_workspace(workspace)
            repository = SQLiteExecutionRuntimeRepository(root / "runtime.sqlite3")
            runtime = LocalExecutionRuntimeService(
                repository=repository,
                repo_store=workspace_store,
                stream_poll_interval=0.01,
            )

            older_session = Session(workspace_id=workspace.workspace_id, title="older-run")
            await runtime.enqueue_run(
                RunRequest(
                    run_id=new_run_id(),
                    workspace_id=workspace.workspace_id,
                    session_id=older_session.session_id,
                    prompt="Older queued run",
                )
            )

            fake_model = FakeModelClient(
                responses=[
                    {
                        "path": "app.txt",
                        "search": "before\n",
                        "replace": "after\n",
                    }
                ]
            )
            agent_core = LocalAgentCoreService(model_client=fake_model, session_store=repository)
            captured_review = None
            original_review_patch = agent_core.review_patch

            async def review_patch_and_capture(session, proposed_action):
                nonlocal captured_review
                captured_review = await original_review_patch(session, proposed_action)
                return captured_review

            agent_core.review_patch = review_patch_and_capture  # type: ignore[method-assign]
            coordinator = AgentCoreCoordinator(
                agent_core=agent_core,
                execution_runtime=runtime,
                session_store=repository,
            )
            repo_intelligence = _PatchRepoIntelligence(workspace)
            app = _LocalCLIApplication(
                agent_core=agent_core,
                coordinator=coordinator,
                execution_runtime=runtime,
                repo_intelligence=repo_intelligence,
                observability=NoopObservabilityService(),
                workspace_store=workspace_store,
            )

            with patch("builtins.print") as mocked_print:
                exit_code = await app.run(
                    [
                        "agent-patch",
                        "--workspace",
                        str(root),
                        "--prompt",
                        "Modify only app.txt by replacing before with after.",
                        "--target-path",
                        "app.txt",
                        "--read-only",
                        "docs/spec.md",
                        "--read-only",
                        "README.md",
                    ]
                )

            runs = await repository.list_runs(workspace.workspace_id)
            succeeded_runs = [run for run in runs if run.status.value == "succeeded"]
            queued_runs = [run for run in runs if run.status.value == "queued"]
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(succeeded_runs), 1)
            self.assertEqual(len(queued_runs), 1)
            outcome = json.loads(mocked_print.call_args.args[0])
            persisted_session = await repository.load_agent_session(succeeded_runs[0].run_id)

            self.assertIsNotNone(captured_review)
            self.assertTrue(captured_review.accepted)
            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            self.assertIsNotNone(persisted_session)
            self.assertEqual(persisted_session.phase.value, "completed")
            self.assertEqual(persisted_session.current_plan.steps[0].status.value, "succeeded")
            self.assertEqual(
                persisted_session.current_plan.steps[0].target_files,
                ("app.txt",),
            )
            self.assertEqual(
                [summary.path for summary in persisted_session.repo_context.reference_file_summaries],
                ["docs/spec.md", "README.md"],
            )
            self.assertEqual(
                repo_intelligence.context_requests[0].reference_paths,
                ("docs/spec.md", "README.md"),
            )
            self.assertEqual(repo_intelligence.context_requests[0].target_paths, ("app.txt",))
            self.assertEqual(persisted_session.failure_history, [])


if __name__ == "__main__":
    unittest.main()
