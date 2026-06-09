from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import BinaryIO

from packages.shared_types import (
    CommandRequest,
    CommandResult,
    ErrorCode,
    ErrorCodeContractError,
    utc_now,
)

DEFAULT_MAX_STDOUT_BYTES = 1024 * 1024
DEFAULT_MAX_STDERR_BYTES = 1024 * 1024
DEFAULT_TERMINATE_GRACE_SECONDS = 0.25


class _CapturedStream:
    def __init__(self, data: str, truncated: bool) -> None:
        self.data = data
        self.truncated = truncated


class LocalCommandExecutor:
    """Local subprocess executor with workspace cwd containment and bounded output capture."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES,
        max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES,
        terminate_grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
    ) -> None:
        self._workspace_root = self._resolve_workspace_root(workspace_root)
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._terminate_grace_seconds = terminate_grace_seconds

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    async def execute(self, request: CommandRequest) -> CommandResult:
        if not request.argv:
            raise ErrorCodeContractError(
                ErrorCode.INVALID_REQUEST,
                "Command argv must not be empty.",
            )

        cwd = self._resolve_cwd(request.cwd)
        env = os.environ.copy()
        if request.env:
            env.update(request.env)

        started_at = utc_now()
        process: asyncio.subprocess.Process | None = None
        stdout_reader: asyncio.Task[_CapturedStream] | None = None
        stderr_reader: asyncio.Task[_CapturedStream] | None = None

        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *request.argv,
                    cwd=str(cwd),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
                raise ErrorCodeContractError(
                    ErrorCode.COMMAND_FAILED,
                    f"Failed to launch command: {exc}",
                    details={"argv0": request.argv[0], "cwd": str(cwd)},
                ) from exc

            stdout_reader = asyncio.create_task(
                self._read_stream(process.stdout, self._max_stdout_bytes)
            )
            stderr_reader = asyncio.create_task(
                self._read_stream(process.stderr, self._max_stderr_bytes)
            )

            timed_out = False
            cancelled = False
            try:
                if request.timeout_seconds is None:
                    await process.wait()
                else:
                    await asyncio.wait_for(process.wait(), timeout=request.timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                await self._terminate_process(process)
            except asyncio.CancelledError:
                cancelled = True
                await self._terminate_process(process)

            stdout_capture = await stdout_reader
            stderr_capture = await stderr_reader
            finished_at = utc_now()
            exit_code = process.returncode

            termination_reason: str | None = None
            if timed_out:
                termination_reason = "timeout"
            elif cancelled:
                termination_reason = "cancelled"
            elif exit_code not in (None, 0):
                termination_reason = "exit_code"

            return CommandResult(
                run_id=request.run_id,
                task_id=request.task_id,
                exit_code=exit_code,
                stdout=stdout_capture.data,
                stderr=stderr_capture.data,
                timed_out=timed_out,
                cancelled=cancelled,
                stdout_truncated=stdout_capture.truncated,
                stderr_truncated=stderr_capture.truncated,
                termination_reason=termination_reason,
                started_at=started_at,
                finished_at=finished_at,
            )
        finally:
            if stdout_reader is not None and stdout_reader.done() is False:
                stdout_reader.cancel()
            if stderr_reader is not None and stderr_reader.done() is False:
                stderr_reader.cancel()

    def _resolve_workspace_root(self, workspace_root: str | Path) -> Path:
        root = Path(workspace_root).expanduser()
        try:
            resolved = root.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_NOT_FOUND,
                f"Workspace root not found: {workspace_root}",
            ) from exc

        if not resolved.is_dir():
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_PATH_INVALID,
                f"Workspace root must be a directory: {workspace_root}",
            )
        return resolved

    def _resolve_cwd(self, raw_cwd: str | None) -> Path:
        if raw_cwd is None:
            return self._workspace_root

        candidate = Path(raw_cwd).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            resolved = (self._workspace_root / candidate).resolve(strict=False)

        if not resolved.is_relative_to(self._workspace_root):
            raise ErrorCodeContractError(
                ErrorCode.AGENT_WRITE_OUTSIDE_WORKSPACE,
                f"Command cwd must stay inside workspace: {raw_cwd}",
                details={
                    "workspace_root": str(self._workspace_root),
                    "resolved_cwd": str(resolved),
                },
            )

        if not resolved.exists() or not resolved.is_dir():
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_PATH_INVALID,
                f"Command cwd must resolve to an existing directory: {raw_cwd}",
                details={"resolved_cwd": str(resolved)},
            )
        return resolved

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return

        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._terminate_grace_seconds)
            return
        except asyncio.TimeoutError:
            pass

        if process.returncode is None:
            process.kill()
            await process.wait()

    async def _read_stream(
        self,
        stream: BinaryIO | None,
        max_bytes: int,
    ) -> _CapturedStream:
        if stream is None:
            return _CapturedStream("", False)

        chunks: list[bytes] = []
        captured_bytes = 0
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            if captured_bytes < max_bytes:
                remaining = max_bytes - captured_bytes
                kept = chunk[:remaining]
                if kept:
                    chunks.append(kept)
                    captured_bytes += len(kept)
                if len(chunk) > remaining:
                    truncated = True
            else:
                truncated = True
        return _CapturedStream(b"".join(chunks).decode("utf-8", errors="replace"), truncated)


__all__ = ["LocalCommandExecutor"]
