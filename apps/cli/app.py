from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import AsyncIterator, Sequence

from packages.shared_types import (
    ApprovalDecision,
    RepoContextRequest,
    RunEvent,
    RunRequest,
    RunResult,
    Session,
    Workspace,
    new_run_id,
)
from services.agent_core import (
    AgentPlan,
    AgentStep,
    LocalAgentRunnerConfig,
    build_local_agent_runner_stack,
    resolve_local_agent_runner_config,
)
from services.execution_runtime import ExecutionRuntimeService, SQLiteExecutionRuntimeRepository
from services.ops_observability import OpsObservabilityService
from services.repo_intelligence import LocalRepoIntelligenceService, RepoIntelligenceService

from apps._local_support import (
    NoopObservabilityService,
    WorkspaceRegistryRepoStore,
    synthesize_run_result,
    wait_for_run_result,
)


class CLIApplication:
    """Thin CLI adapter over the platform services."""

    async def run(self, argv: Sequence[str]) -> int:
        raise NotImplementedError

    async def submit_run(self, request: RunRequest) -> str:
        raise NotImplementedError

    async def stream_run(self, run_id: str) -> AsyncIterator[RunEvent]:
        raise NotImplementedError

    async def await_result(self, run_id: str) -> RunResult:
        raise NotImplementedError

    async def submit_approval(self, decision: ApprovalDecision) -> None:
        raise NotImplementedError


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core")
    parser.add_argument("--config")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--system-prompt")
    parser.add_argument("--api-key", action="append", default=[])
    parser.add_argument("--openai-api-key")
    parser.add_argument("--anthropic-api-key")
    parser.add_argument("--openrouter-api-key")
    parser.add_argument("--runtime-db-path")
    parser.add_argument("--stream-poll-interval", type=float)
    parser.add_argument("--fallback-test-command")

    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("agent-plan")
    plan_parser.add_argument("--workspace", required=True)
    plan_parser.add_argument("--prompt", required=True)
    plan_parser.add_argument("--target-path", action="append", default=[])
    plan_parser.add_argument("--max-files", type=int, default=None)

    run_parser = subparsers.add_parser("agent-run")
    run_parser.add_argument("--workspace", required=True)
    run_parser.add_argument("--prompt", required=True)
    run_parser.add_argument("--target-path", action="append", default=[])
    run_parser.add_argument("--read-only", action="append", default=[])
    run_parser.add_argument("--max-files", type=int, default=None)
    run_parser.add_argument("--lease-seconds", type=int, default=30)
    run_parser.add_argument("--worker-id", default="cli-worker")

    patch_parser = subparsers.add_parser("agent-patch")
    patch_parser.add_argument("--workspace", required=True)
    patch_parser.add_argument("--prompt", required=True)
    patch_parser.add_argument("--target-path", action="append", required=True)
    patch_parser.add_argument("--read-only", action="append", default=[])
    patch_parser.add_argument("--max-files", type=int, default=None)
    patch_parser.add_argument("--lease-seconds", type=int, default=30)
    patch_parser.add_argument("--worker-id", default="cli-worker")

    return parser


def resolve_local_cli_runner_config(
    argv: Sequence[str],
    *,
    env=None,
) -> LocalAgentRunnerConfig:
    args = build_cli_parser().parse_args(list(argv))
    return resolve_local_agent_runner_config(
        workspace_root=args.workspace,
        env=env,
        config_path=args.config,
        cli_overrides={
            "provider": args.provider,
            "model": args.model,
            "system_prompt": args.system_prompt,
            "api_keys": tuple(args.api_key),
            "openai_api_key": args.openai_api_key,
            "anthropic_api_key": args.anthropic_api_key,
            "openrouter_api_key": args.openrouter_api_key,
            "runtime_db_path": args.runtime_db_path,
            "stream_poll_interval": args.stream_poll_interval,
            "fallback_test_command": args.fallback_test_command,
        },
    )


class _LocalCLIApplication(CLIApplication):
    def __init__(
        self,
        *,
        agent_core,
        coordinator=None,
        execution_runtime: ExecutionRuntimeService,
        repo_intelligence: RepoIntelligenceService,
        observability: OpsObservabilityService,
        workspace_store: WorkspaceRegistryRepoStore | None = None,
    ) -> None:
        self._agent_core = agent_core
        self._coordinator = coordinator
        self._execution_runtime = execution_runtime
        self._repo_intelligence = repo_intelligence
        self._observability = observability
        self._workspace_store = workspace_store

    async def run(self, argv: Sequence[str]) -> int:
        args = build_cli_parser().parse_args(list(argv))

        if args.command == "agent-plan":
            return await self._run_plan_command(
                workspace_path=args.workspace,
                prompt=args.prompt,
                target_paths=tuple(args.target_path),
                max_files=args.max_files,
            )

        if args.command == "agent-run":
            return await self._run_agent_command(
                workspace_path=args.workspace,
                prompt=args.prompt,
                target_paths=tuple(args.target_path),
                reference_paths=tuple(args.read_only),
                max_files=args.max_files,
                lease_seconds=args.lease_seconds,
                worker_id=args.worker_id,
            )

        if args.command == "agent-patch":
            return await self._run_patch_command(
                workspace_path=args.workspace,
                prompt=args.prompt,
                target_paths=tuple(args.target_path),
                reference_paths=tuple(args.read_only),
                max_files=args.max_files,
                lease_seconds=args.lease_seconds,
                worker_id=args.worker_id,
            )

        raise ValueError(f"Unsupported CLI command: {args.command!r}")

    async def submit_run(self, request: RunRequest) -> str:
        return await self._execution_runtime.enqueue_run(request)

    async def stream_run(self, run_id: str) -> AsyncIterator[RunEvent]:
        async for event in self._execution_runtime.stream_events(run_id):
            yield event

    async def await_result(self, run_id: str) -> RunResult:
        repository = self._require_repository()
        return await wait_for_run_result(repository, run_id)

    async def submit_approval(self, decision: ApprovalDecision) -> None:
        record = getattr(self._execution_runtime, "record_approval_decision", None)
        if not callable(record):
            raise NotImplementedError("Execution runtime does not support approval decisions")
        await record(decision)

    async def _run_plan_command(
        self,
        *,
        workspace_path: str,
        prompt: str,
        target_paths: tuple[str, ...],
        max_files: int | None,
    ) -> int:
        workspace = await self._inspect_and_register_workspace(workspace_path)
        run_id = new_run_id()
        context = await self._repo_intelligence.build_context(
            RepoContextRequest(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                prompt=prompt,
                target_paths=target_paths,
                max_files=max_files,
            )
        )
        session = self._agent_core.create_session(
            run_id=run_id,
            workspace_id=workspace.workspace_id,
            user_request=prompt,
            repo_context=context,
        )
        plan = await self._agent_core.create_plan(session)
        print(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False))
        return 0

    async def _run_agent_command(
        self,
        *,
        workspace_path: str,
        prompt: str,
        target_paths: tuple[str, ...],
        reference_paths: tuple[str, ...],
        max_files: int | None,
        lease_seconds: int,
        worker_id: str,
    ) -> int:
        workspace = await self._inspect_and_register_workspace(workspace_path)
        ui_session = Session(workspace_id=workspace.workspace_id, title="agent-cli")
        run_id = new_run_id()

        await self.submit_run(
            RunRequest(
                run_id=run_id,
                workspace_id=workspace.workspace_id,
                session_id=ui_session.session_id,
                prompt=prompt,
                target_paths=target_paths,
            )
        )
        await self._claim_run(str(run_id), worker_id, lease_seconds)

        initial_context = await self._repo_intelligence.build_context(
            RepoContextRequest(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                prompt=prompt,
                target_paths=target_paths,
                reference_paths=reference_paths,
                max_files=max_files,
            )
        )
        session = self._agent_core.create_session(
            run_id=run_id,
            workspace_id=workspace.workspace_id,
            user_request=prompt,
            repo_context=initial_context,
        )
        await self._agent_core.create_plan(session)
        current_session = await self._load_or_reconstruct_session(session)

        coordinator = self._require_coordinator()
        outcome = await coordinator.run(current_session)
        while outcome.status == "context_requested":
            requested_context = outcome.requested_context or target_paths
            next_context = await self._repo_intelligence.build_context(
                RepoContextRequest(
                    workspace_id=workspace.workspace_id,
                    run_id=run_id,
                    prompt=prompt,
                    target_paths=tuple(requested_context),
                    reference_paths=reference_paths,
                    max_files=max_files,
                )
            )
            outcome = await coordinator.resume_after_context(str(run_id), next_context)

        print(json.dumps(outcome.to_dict(), indent=2, ensure_ascii=False))
        if outcome.status in {"completed", "summarized"}:
            return 0
        if outcome.status == "approval_requested":
            return 2
        if outcome.status == "needs_recovery":
            return 3
        return 1

    async def _run_patch_command(
        self,
        *,
        workspace_path: str,
        prompt: str,
        target_paths: tuple[str, ...],
        reference_paths: tuple[str, ...],
        max_files: int | None,
        lease_seconds: int,
        worker_id: str,
    ) -> int:
        workspace = await self._inspect_and_register_workspace(workspace_path)
        ui_session = Session(workspace_id=workspace.workspace_id, title="agent-cli-patch")
        run_id = new_run_id()

        await self.submit_run(
            RunRequest(
                run_id=run_id,
                workspace_id=workspace.workspace_id,
                session_id=ui_session.session_id,
                prompt=prompt,
                target_paths=target_paths,
            )
        )
        await self._claim_run(str(run_id), worker_id, lease_seconds)

        repo_context = await self._repo_intelligence.build_context(
            RepoContextRequest(
                workspace_id=workspace.workspace_id,
                run_id=run_id,
                prompt=prompt,
                target_paths=target_paths,
                reference_paths=reference_paths,
                max_files=max_files,
            )
        )
        session = self._agent_core.create_session(
            run_id=run_id,
            workspace_id=workspace.workspace_id,
            user_request=prompt,
            repo_context=repo_context,
            current_plan=AgentPlan(
                goal=prompt,
                steps=[
                    AgentStep(
                        step_id="step_1",
                        kind="patch",
                        description=prompt,
                        target_files=target_paths,
                        rationale="Direct patch-mode CLI run",
                    ),
                    AgentStep(
                        step_id="step_2",
                        kind="complete",
                        description="Complete the requested patch task",
                    ),
                ],
            ),
        )

        coordinator = self._require_coordinator()
        outcome = await coordinator.run(session)
        print(json.dumps(outcome.to_dict(), indent=2, ensure_ascii=False))
        if outcome.status in {"completed", "summarized"}:
            return 0
        if outcome.status == "approval_requested":
            return 2
        if outcome.status == "needs_recovery":
            return 3
        return 1

    async def _inspect_and_register_workspace(self, workspace_path: str) -> Workspace:
        workspace = Workspace(root_path=str(Path(workspace_path).resolve()))
        inspected = await self._repo_intelligence.inspect_workspace(workspace)
        if self._workspace_store is not None:
            self._workspace_store.register_workspace(inspected)
        return inspected

    async def _claim_run(self, run_id: str, worker_id: str, lease_seconds: int) -> None:
        claim_run = getattr(self._execution_runtime, "claim_run", None)
        if callable(claim_run):
            claimed = await claim_run(run_id, worker_id, lease_seconds)
            if claimed is None:
                raise RuntimeError(f"Run {run_id} could not be claimed")
            return

        claim_next = getattr(self._execution_runtime, "claim_next_run", None)
        if not callable(claim_next):
            raise RuntimeError("Execution runtime does not support local claim_next_run")
        claimed = await claim_next(worker_id, lease_seconds)
        if claimed is None:
            raise RuntimeError("No queued run was available to claim")
        if str(claimed.run_id) != run_id:
            raise RuntimeError(
                f"Claimed run {claimed.run_id} but expected to claim {run_id}"
            )

    async def _load_or_reconstruct_session(self, session):
        repository = self._require_repository()
        persisted = await repository.load_agent_session(session.run_id)
        if persisted is not None:
            return persisted
        return session

    def _require_repository(self) -> SQLiteExecutionRuntimeRepository:
        repository = getattr(self._execution_runtime, "repository", None)
        if not isinstance(repository, SQLiteExecutionRuntimeRepository):
            raise RuntimeError("CLI local adapter requires a LocalExecutionRuntimeService repository")
        return repository

    def _require_coordinator(self):
        coordinator = self._coordinator
        if coordinator is None:
            raise RuntimeError("CLI local adapter requires an attached coordinator")
        return coordinator


def create_cli_application(
    agent_core,
    execution_runtime: ExecutionRuntimeService,
    repo_intelligence: RepoIntelligenceService,
    observability: OpsObservabilityService,
) -> CLIApplication:
    """Create the CLI adapter from injected services."""
    return _LocalCLIApplication(
        agent_core=agent_core,
        coordinator=None,
        execution_runtime=execution_runtime,
        repo_intelligence=repo_intelligence,
        observability=observability,
    )


def create_local_cli_application_from_env() -> CLIApplication:
    config = resolve_local_agent_runner_config(workspace_root=Path.cwd())
    return create_local_cli_application_from_config(config)


def create_local_cli_application_from_config(
    config: LocalAgentRunnerConfig,
) -> CLIApplication:
    workspace_store = WorkspaceRegistryRepoStore()
    repo_intelligence = LocalRepoIntelligenceService()
    observability = NoopObservabilityService()
    stack = build_local_agent_runner_stack(
        config=config,
        repo_store=workspace_store,
        repo_intelligence=repo_intelligence,
    )
    return _LocalCLIApplication(
        agent_core=stack.agent_core,
        coordinator=stack.coordinator,
        execution_runtime=stack.execution_runtime,
        repo_intelligence=repo_intelligence,
        observability=observability,
        workspace_store=workspace_store,
    )


async def _main(argv: Sequence[str]) -> int:
    config = resolve_local_cli_runner_config(argv)
    app = create_local_cli_application_from_config(config)
    return await app.run(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
