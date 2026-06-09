import importlib
import json
import unittest

from packages.shared_types import (
    Artifact,
    ArtifactType,
    CommandRequest,
    CommandResult,
    ErrorCode,
    EventType,
    Run,
    RunResult,
    RunStatus,
    Session,
    Task,
    TaskStatus,
    Workspace,
    build_run_event,
    new_approval_id,
    new_artifact_id,
    new_event_id,
    new_run_id,
    new_session_id,
    new_task_id,
    new_workspace_id,
    utc_now,
)


class TestSharedTypesContracts(unittest.TestCase):
    def test_error_code_is_exported_with_stable_value(self):
        self.assertEqual(ErrorCode.INVALID_REQUEST.value, "invalid_request")

    def test_error_code_values_are_unique(self):
        values = [error_code.value for error_code in ErrorCode]
        self.assertEqual(len(values), len(set(values)))

    def test_generated_ids_are_string_serializable(self):
        generated_ids = [
            new_workspace_id(),
            new_session_id(),
            new_run_id(),
            new_task_id(),
            new_artifact_id(),
            new_approval_id(),
            new_event_id(),
        ]

        expected_prefixes = [
            "ws_",
            "session_",
            "run_",
            "task_",
            "artifact_",
            "approval_",
            "event_",
        ]

        for generated_id, expected_prefix in zip(generated_ids, expected_prefixes):
            self.assertIsInstance(generated_id, str)
            self.assertTrue(generated_id.startswith(expected_prefix))

    def test_models_serialize_nested_contracts(self):
        workspace = Workspace(root_path="/tmp/repo")
        session = Session(workspace_id=workspace.workspace_id, title="Shared contracts")
        started_at = utc_now()
        finished_at = utc_now()

        run = Run(
            workspace_id=workspace.workspace_id,
            session_id=session.session_id,
            prompt="Implement Step 0 foundation",
            status=RunStatus.RUNNING,
            worker_id="worker-1",
            attempt=2,
            lease_expires_at=finished_at,
            last_heartbeat_at=started_at,
            started_at=started_at,
        )
        task = Task(
            run_id=run.run_id,
            title="Define shared contracts",
            status=TaskStatus.SUCCEEDED,
            started_at=started_at,
            finished_at=finished_at,
        )
        artifact = Artifact(
            run_id=run.run_id,
            task_id=task.task_id,
            artifact_type=ArtifactType.LOG,
            label="unit test output",
        )
        command_request = CommandRequest(
            run_id=run.run_id,
            task_id=task.task_id,
            argv=("pytest", "-q"),
            cwd="/tmp/repo",
            env={"PYTHONPATH": "/tmp/repo"},
            timeout_seconds=30,
        )
        command_result = CommandResult(
            run_id=run.run_id,
            task_id=task.task_id,
            exit_code=None,
            stdout="partial stdout",
            stderr="partial stderr",
            timed_out=True,
            cancelled=False,
            stdout_truncated=True,
            stderr_truncated=False,
            termination_reason="timeout",
            started_at=started_at,
            finished_at=finished_at,
        )
        result = RunResult(
            run_id=run.run_id,
            status=RunStatus.SUCCEEDED,
            summary="Contracts created",
            artifacts=(artifact,),
            started_at=started_at,
            finished_at=finished_at,
        )

        payload = result.to_dict()
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["artifacts"][0]["artifact_type"], "log")
        self.assertTrue(payload["artifacts"][0]["artifact_id"].startswith("artifact_"))
        self.assertEqual(run.to_dict()["worker_id"], "worker-1")
        self.assertEqual(run.to_dict()["attempt"], 2)
        self.assertTrue(run.to_dict()["lease_expires_at"].endswith("Z"))
        self.assertTrue(payload["finished_at"].endswith("Z"))
        self.assertEqual(command_request.to_dict()["env"], {"PYTHONPATH": "/tmp/repo"})
        self.assertEqual(command_result.to_dict()["timed_out"], True)
        self.assertEqual(command_result.to_dict()["stdout_truncated"], True)
        self.assertEqual(command_result.to_dict()["termination_reason"], "timeout")

        event = build_run_event(
            run.run_id,
            EventType.COMMAND_TIMEOUT,
            sequence=3,
            task_id=task.task_id,
            task_status=TaskStatus.SUCCEEDED,
            artifact_id=artifact.artifact_id,
            payload={"argv": ["pytest", "-q"]},
        )
        event_payload = event.to_dict()
        self.assertEqual(event_payload["event_type"], "command.timeout")
        self.assertEqual(event_payload["sequence"], 3)
        self.assertEqual(event_payload["task_status"], "succeeded")
        self.assertEqual(event_payload["payload"]["argv"], ["pytest", "-q"])

        self.assertEqual(EventType.COMMAND_CANCELLED.value, "command.cancelled")

        round_tripped = json.loads(event.to_json())
        self.assertEqual(round_tripped["run_id"], str(run.run_id))

    def test_placeholder_modules_still_import(self):
        modules = [
            "apps.api.app",
            "apps.cli.app",
            "apps.dashboard.app",
            "packages.provider_adapters.llm",
            "packages.provider_adapters.tools",
            "services.agent_core.service",
            "services.execution_runtime.service",
            "services.ops_observability.service",
            "services.repo_intelligence.service",
        ]

        for module_name in modules:
            imported = importlib.import_module(module_name)
            self.assertIsNotNone(imported)


if __name__ == "__main__":
    unittest.main()
