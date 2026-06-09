from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from packages.shared_types import (
    CommandRequest,
    ErrorCode,
    ErrorCodeContractError,
    new_run_id,
    new_task_id,
)
from services.execution_runtime.command import LocalCommandExecutor


class TestLocalCommandExecutor(unittest.IsolatedAsyncioTestCase):
    def _make_request(
        self,
        *,
        argv: tuple[str, ...],
        cwd: str | None = None,
        timeout_seconds: int | float | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandRequest:
        return CommandRequest(
            run_id=new_run_id(),
            task_id=new_task_id(),
            argv=argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            env=env,
        )

    async def test_command_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = LocalCommandExecutor(tmpdir)
            request = self._make_request(
                argv=(sys.executable, "-c", "import sys; sys.stdout.write('ok'); sys.stderr.write('warn')"),
            )

            result = await executor.execute(request)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout, "ok")
            self.assertEqual(result.stderr, "warn")
            self.assertFalse(result.timed_out)
            self.assertFalse(result.cancelled)
            self.assertFalse(result.stdout_truncated)
            self.assertFalse(result.stderr_truncated)
            self.assertIsNone(result.termination_reason)

    async def test_non_zero_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = LocalCommandExecutor(tmpdir)
            request = self._make_request(
                argv=(sys.executable, "-c", "import sys; sys.exit(7)"),
            )

            result = await executor.execute(request)

            self.assertEqual(result.exit_code, 7)
            self.assertEqual(result.termination_reason, "exit_code")
            self.assertFalse(result.timed_out)
            self.assertFalse(result.cancelled)

    async def test_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = LocalCommandExecutor(tmpdir)
            request = self._make_request(
                argv=(sys.executable, "-c", "import time; time.sleep(1)"),
                timeout_seconds=0.05,
            )

            result = await executor.execute(request)

            self.assertTrue(result.timed_out)
            self.assertEqual(result.termination_reason, "timeout")
            self.assertIsNotNone(result.exit_code)

    async def test_stdout_truncation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = LocalCommandExecutor(tmpdir, max_stdout_bytes=5)
            request = self._make_request(
                argv=(sys.executable, "-c", "import sys; sys.stdout.write('abcdefghij')"),
            )

            result = await executor.execute(request)

            self.assertEqual(result.stdout, "abcde")
            self.assertTrue(result.stdout_truncated)
            self.assertFalse(result.stderr_truncated)

    async def test_stderr_truncation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = LocalCommandExecutor(tmpdir, max_stderr_bytes=5)
            request = self._make_request(
                argv=(sys.executable, "-c", "import sys; sys.stderr.write('abcdefghij')"),
            )

            result = await executor.execute(request)

            self.assertEqual(result.stderr, "abcde")
            self.assertTrue(result.stderr_truncated)
            self.assertFalse(result.stdout_truncated)

    async def test_cwd_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            outside = root / "outside"
            outside.mkdir()
            executor = LocalCommandExecutor(workspace)
            request = self._make_request(
                argv=(sys.executable, "-c", "print('nope')"),
                cwd=str(outside),
            )

            with self.assertRaises(ErrorCodeContractError) as context:
                await executor.execute(request)

            self.assertEqual(context.exception.error_code, ErrorCode.AGENT_WRITE_OUTSIDE_WORKSPACE)

    async def test_env_is_passed_to_child_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = LocalCommandExecutor(tmpdir)
            request = self._make_request(
                argv=(sys.executable, "-c", "import os,sys; sys.stdout.write(os.environ['MY_TEST_ENV'])"),
                env={"MY_TEST_ENV": "hello"},
            )

            result = await executor.execute(request)

            self.assertEqual(result.stdout, "hello")
            self.assertEqual(result.exit_code, 0)

    async def test_empty_argv_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = LocalCommandExecutor(tmpdir)
            request = self._make_request(argv=())

            with self.assertRaises(ErrorCodeContractError) as context:
                await executor.execute(request)

            self.assertEqual(context.exception.error_code, ErrorCode.INVALID_REQUEST)


if __name__ == "__main__":
    unittest.main()
