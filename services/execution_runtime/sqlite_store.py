from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Iterator, Mapping, Sequence

from packages.shared_types import (
    ApprovalRequest,
    ApprovalId,
    Artifact,
    ArtifactId,
    ArtifactType,
    EntityNotFoundError,
    EventId,
    EventType,
    JSONValue,
    Run,
    RunEvent,
    RunId,
    RunStatus,
    SessionId,
    TaskId,
    TaskStatus,
    WorkspaceId,
    build_run_event,
    build_run_status_event,
    utc_now,
)

from .events import json_dumps, json_loads, parse_datetime, run_status_event_type, serialize_datetime
from .state_machine import TERMINAL_RUN_STATUSES, validate_run_transition


class SQLiteExecutionRuntimeRepository:
    """SQLite-backed durable store for local execution runtime state."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def create_run(
        self,
        run: Run,
        *,
        events: Sequence[RunEvent] = (),
    ) -> tuple[RunEvent, ...]:
        return await asyncio.to_thread(self._create_run_sync, run, tuple(events))

    async def get_run(self, run_id: RunId | str) -> Run | None:
        return await asyncio.to_thread(self._get_run_sync, RunId(str(run_id)))

    async def update_run_status(
        self,
        run_id: RunId | str,
        status: RunStatus,
        *,
        event_type: EventType | None = None,
        message: str | None = None,
        payload: Mapping[str, JSONValue] | None = None,
        now: datetime | None = None,
    ) -> tuple[Run, RunEvent]:
        return await asyncio.to_thread(
            self._update_run_status_sync,
            RunId(str(run_id)),
            status,
            event_type,
            message,
            dict(payload or {}),
            now,
        )

    async def append_event_with_sequence(
        self,
        run_id: RunId | str,
        event: RunEvent,
    ) -> RunEvent:
        return await asyncio.to_thread(
            self._append_event_with_sequence_sync,
            RunId(str(run_id)),
            event,
        )

    async def list_events(
        self,
        run_id: RunId | str,
        *,
        after_sequence: int = 0,
    ) -> tuple[RunEvent, ...]:
        return await asyncio.to_thread(
            self._list_events_sync,
            RunId(str(run_id)),
            after_sequence,
        )

    async def claim_next_run(
        self,
        worker_id: str,
        lease_seconds: int,
    ) -> Run | None:
        return await asyncio.to_thread(self._claim_next_run_sync, worker_id, lease_seconds)

    async def heartbeat_run(
        self,
        run_id: RunId | str,
        worker_id: str,
        lease_seconds: int,
    ) -> Run | None:
        return await asyncio.to_thread(
            self._heartbeat_run_sync,
            RunId(str(run_id)),
            worker_id,
            lease_seconds,
        )

    async def recover_stale_runs(
        self,
        now: datetime | None = None,
    ) -> tuple[Run, ...]:
        return await asyncio.to_thread(self._recover_stale_runs_sync, now or utc_now())

    async def create_artifact(self, artifact: Artifact) -> None:
        await asyncio.to_thread(self._create_artifact_sync, artifact)

    async def list_artifacts(self, run_id: RunId | str) -> tuple[Artifact, ...]:
        return await asyncio.to_thread(self._list_artifacts_sync, RunId(str(run_id)))

    async def create_approval_request(self, request: ApprovalRequest) -> RunEvent:
        return await asyncio.to_thread(self._create_approval_request_sync, request)

    async def list_approval_requests(self, run_id: RunId | str) -> tuple[ApprovalRequest, ...]:
        return await asyncio.to_thread(self._list_approval_requests_sync, RunId(str(run_id)))

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    last_heartbeat_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    message TEXT,
                    run_status TEXT,
                    task_id TEXT,
                    task_status TEXT,
                    artifact_id TEXT,
                    approval_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT,
                    artifact_type TEXT NOT NULL,
                    label TEXT,
                    uri TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT,
                    patch_id TEXT,
                    reason TEXT NOT NULL,
                    command_argv_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    approved INTEGER,
                    decided_at TEXT,
                    reviewer TEXT,
                    comment TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_runs_status_created
                    ON runs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_runs_status_lease
                    ON runs(status, lease_expires_at);
                CREATE INDEX IF NOT EXISTS idx_run_events_sequence
                    ON run_events(run_id, sequence);
                """
            )
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._db_path),
            isolation_level=None,
            timeout=30.0,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _transaction(self, mode: str = "IMMEDIATE") -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute(f"BEGIN {mode}")
        try:
            yield connection
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
        finally:
            connection.close()

    def _create_run_sync(
        self,
        run: Run,
        events: Sequence[RunEvent],
    ) -> tuple[RunEvent, ...]:
        with self._transaction("IMMEDIATE") as connection:
            self._insert_run(connection, run)
            return tuple(
                self._append_event_with_sequence_in_tx(connection, run.run_id, event)
                for event in events
            )

    def _get_run_sync(self, run_id: RunId) -> Run | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return self._run_from_row(row)

    def _update_run_status_sync(
        self,
        run_id: RunId,
        status: RunStatus,
        event_type: EventType | None,
        message: str | None,
        payload: Mapping[str, JSONValue],
        now: datetime | None,
    ) -> tuple[Run, RunEvent]:
        now = now or utc_now()
        with self._transaction("IMMEDIATE") as connection:
            row = self._select_run_for_update(connection, run_id)
            current = self._run_from_row(row)
            validate_run_transition(current.status, status)

            updated = replace(
                current,
                status=status,
                worker_id=current.worker_id if status == RunStatus.RUNNING else None,
                lease_expires_at=current.lease_expires_at if status == RunStatus.RUNNING else None,
                last_heartbeat_at=current.last_heartbeat_at if status == RunStatus.RUNNING else None,
                updated_at=now,
                finished_at=now if status in TERMINAL_RUN_STATUSES else current.finished_at,
            )
            self._update_run_row(connection, updated)

            event = build_run_event(
                run_id=run_id,
                event_type=event_type or run_status_event_type(status),
                message=message,
                run_status=status,
                payload=payload,
            )
            persisted_event = self._append_event_with_sequence_in_tx(connection, run_id, event)
            return updated, persisted_event

    def _append_event_with_sequence_sync(self, run_id: RunId, event: RunEvent) -> RunEvent:
        with self._transaction("IMMEDIATE") as connection:
            self._select_run_for_update(connection, run_id)
            return self._append_event_with_sequence_in_tx(connection, run_id, event)

    def _list_events_sync(self, run_id: RunId, after_sequence: int) -> tuple[RunEvent, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (str(run_id), after_sequence),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._event_from_row(row) for row in rows)

    def _claim_next_run_sync(self, worker_id: str, lease_seconds: int) -> Run | None:
        now = utc_now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._transaction("IMMEDIATE") as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE status = ?
                ORDER BY created_at ASC, run_id ASC
                LIMIT 1
                """,
                (RunStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None

            current = self._run_from_row(row)
            claimed = replace(
                current,
                status=RunStatus.RUNNING,
                worker_id=worker_id,
                attempt=current.attempt + 1,
                lease_expires_at=lease_expires_at,
                last_heartbeat_at=now,
                updated_at=now,
                started_at=current.started_at or now,
                finished_at=None,
            )
            updated_rows = connection.execute(
                """
                UPDATE runs
                SET status = ?, worker_id = ?, attempt = ?, lease_expires_at = ?,
                    last_heartbeat_at = ?, updated_at = ?, started_at = ?, finished_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    claimed.status.value,
                    claimed.worker_id,
                    claimed.attempt,
                    serialize_datetime(claimed.lease_expires_at),
                    serialize_datetime(claimed.last_heartbeat_at),
                    serialize_datetime(claimed.updated_at),
                    serialize_datetime(claimed.started_at),
                    serialize_datetime(claimed.finished_at),
                    str(claimed.run_id),
                    RunStatus.QUEUED.value,
                ),
            ).rowcount
            if updated_rows != 1:
                return None

            event = build_run_status_event(
                claimed,
                EventType.RUN_STARTED,
                payload={"worker_id": worker_id, "attempt": claimed.attempt},
            )
            self._append_event_with_sequence_in_tx(connection, claimed.run_id, event)
            return claimed

    def _heartbeat_run_sync(
        self,
        run_id: RunId,
        worker_id: str,
        lease_seconds: int,
    ) -> Run | None:
        now = utc_now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._transaction("IMMEDIATE") as connection:
            row = self._select_run_for_update(connection, run_id)
            current = self._run_from_row(row)
            if current.status != RunStatus.RUNNING or current.worker_id != worker_id:
                return None

            updated = replace(
                current,
                lease_expires_at=lease_expires_at,
                last_heartbeat_at=now,
                updated_at=now,
            )
            self._update_run_row(connection, updated)
            return updated

    def _recover_stale_runs_sync(self, now: datetime) -> tuple[Run, ...]:
        recovered_runs: list[Run] = []
        with self._transaction("IMMEDIATE") as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                ORDER BY lease_expires_at ASC, run_id ASC
                """,
                (RunStatus.RUNNING.value, serialize_datetime(now)),
            ).fetchall()
            for row in rows:
                current = self._run_from_row(row)
                # TODO: Before enabling side-effecting command execution, recovery strategy must be revisited.
                # Stale RUNNING runs should become FAILED or RECOVERABLE unless task-level checkpoints exist.
                recovered = replace(
                    current,
                    status=RunStatus.QUEUED,
                    worker_id=None,
                    lease_expires_at=None,
                    last_heartbeat_at=None,
                    updated_at=now,
                )
                self._update_run_row(connection, recovered)
                event = build_run_status_event(
                    recovered,
                    EventType.RUN_QUEUED,
                    message="Recovered stale run after lease expiry.",
                    payload={
                        "recovered_from_status": current.status.value,
                        "previous_worker_id": current.worker_id,
                        "attempt": current.attempt,
                    },
                )
                self._append_event_with_sequence_in_tx(connection, recovered.run_id, event)
                recovered_runs.append(recovered)
        return tuple(recovered_runs)

    def _create_artifact_sync(self, artifact: Artifact) -> None:
        with self._transaction("IMMEDIATE") as connection:
            connection.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, run_id, task_id, artifact_type, label, uri, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(artifact.artifact_id),
                    str(artifact.run_id),
                    str(artifact.task_id) if artifact.task_id is not None else None,
                    artifact.artifact_type.value,
                    artifact.label,
                    artifact.uri,
                    serialize_datetime(artifact.created_at),
                ),
            )

    def _list_artifacts_sync(self, run_id: RunId) -> tuple[Artifact, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE run_id = ?
                ORDER BY created_at ASC, artifact_id ASC
                """,
                (str(run_id),),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._artifact_from_row(row) for row in rows)

    def _create_approval_request_sync(self, request: ApprovalRequest) -> RunEvent:
        with self._transaction("IMMEDIATE") as connection:
            row = self._select_run_for_update(connection, request.run_id)
            current = self._run_from_row(row)
            validate_run_transition(current.status, RunStatus.WAITING_FOR_APPROVAL)

            connection.execute(
                """
                INSERT INTO approvals (
                    approval_id, run_id, task_id, patch_id, reason, command_argv_json,
                    created_at, expires_at, approved, decided_at, reviewer, comment
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
                """,
                (
                    str(request.approval_id),
                    str(request.run_id),
                    str(request.task_id) if request.task_id is not None else None,
                    str(request.patch_id) if request.patch_id is not None else None,
                    request.reason,
                    json_dumps({"command_argv": list(request.command_argv)}),
                    serialize_datetime(request.created_at),
                    serialize_datetime(request.expires_at),
                ),
            )

            suspended = replace(
                current,
                status=RunStatus.WAITING_FOR_APPROVAL,
                worker_id=None,
                lease_expires_at=None,
                last_heartbeat_at=None,
                updated_at=request.created_at,
                finished_at=None,
            )
            self._update_run_row(connection, suspended)
            event = build_run_event(
                run_id=current.run_id,
                event_type=EventType.APPROVAL_REQUESTED,
                message=request.reason,
                run_status=RunStatus.WAITING_FOR_APPROVAL,
                task_id=request.task_id,
                approval_id=request.approval_id,
                artifact_id=request.patch_id,
                payload={
                    "approval_id": str(request.approval_id),
                    "command_argv": list(request.command_argv),
                    "expires_at": serialize_datetime(request.expires_at),
                },
            )
            return self._append_event_with_sequence_in_tx(connection, current.run_id, event)

    def _list_approval_requests_sync(self, run_id: RunId) -> tuple[ApprovalRequest, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM approvals
                WHERE run_id = ?
                ORDER BY created_at ASC, approval_id ASC
                """,
                (str(run_id),),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._approval_request_from_row(row) for row in rows)

    def _insert_run(self, connection: sqlite3.Connection, run: Run) -> None:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, workspace_id, session_id, prompt, status, worker_id, attempt,
                lease_expires_at, last_heartbeat_at, created_at, updated_at, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(run.run_id),
                str(run.workspace_id),
                str(run.session_id),
                run.prompt,
                run.status.value,
                run.worker_id,
                run.attempt,
                serialize_datetime(run.lease_expires_at),
                serialize_datetime(run.last_heartbeat_at),
                serialize_datetime(run.created_at),
                serialize_datetime(run.updated_at),
                serialize_datetime(run.started_at),
                serialize_datetime(run.finished_at),
            ),
        )

    def _update_run_row(self, connection: sqlite3.Connection, run: Run) -> None:
        connection.execute(
            """
            UPDATE runs
            SET workspace_id = ?, session_id = ?, prompt = ?, status = ?, worker_id = ?,
                attempt = ?, lease_expires_at = ?, last_heartbeat_at = ?, created_at = ?,
                updated_at = ?, started_at = ?, finished_at = ?
            WHERE run_id = ?
            """,
            (
                str(run.workspace_id),
                str(run.session_id),
                run.prompt,
                run.status.value,
                run.worker_id,
                run.attempt,
                serialize_datetime(run.lease_expires_at),
                serialize_datetime(run.last_heartbeat_at),
                serialize_datetime(run.created_at),
                serialize_datetime(run.updated_at),
                serialize_datetime(run.started_at),
                serialize_datetime(run.finished_at),
                str(run.run_id),
            ),
        )

    def _select_run_for_update(self, connection: sqlite3.Connection, run_id: RunId) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        if row is None:
            raise EntityNotFoundError("run", str(run_id))
        return row

    def _append_event_with_sequence_in_tx(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
        event: RunEvent,
    ) -> RunEvent:
        if str(event.run_id) != str(run_id):
            raise ValueError("Event run_id must match the target run")

        next_sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()[0]
        persisted = replace(event, sequence=int(next_sequence))
        connection.execute(
            """
            INSERT INTO run_events (
                run_id, sequence, event_id, event_type, message, run_status, task_id,
                task_status, artifact_id, approval_id, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(persisted.run_id),
                persisted.sequence,
                str(persisted.event_id),
                persisted.event_type.value,
                persisted.message,
                persisted.run_status.value if persisted.run_status is not None else None,
                str(persisted.task_id) if persisted.task_id is not None else None,
                persisted.task_status.value if persisted.task_status is not None else None,
                str(persisted.artifact_id) if persisted.artifact_id is not None else None,
                str(persisted.approval_id) if persisted.approval_id is not None else None,
                json_dumps(dict(persisted.payload)),
                serialize_datetime(persisted.created_at),
            ),
        )
        return persisted

    def _run_from_row(self, row: sqlite3.Row) -> Run:
        return Run(
            run_id=RunId(row["run_id"]),
            workspace_id=WorkspaceId(row["workspace_id"]),
            session_id=SessionId(row["session_id"]),
            prompt=row["prompt"],
            status=RunStatus(row["status"]),
            worker_id=row["worker_id"],
            attempt=int(row["attempt"]),
            lease_expires_at=parse_datetime(row["lease_expires_at"]),
            last_heartbeat_at=parse_datetime(row["last_heartbeat_at"]),
            created_at=parse_datetime(row["created_at"]) or utc_now(),
            updated_at=parse_datetime(row["updated_at"]) or utc_now(),
            started_at=parse_datetime(row["started_at"]),
            finished_at=parse_datetime(row["finished_at"]),
        )

    def _event_from_row(self, row: sqlite3.Row) -> RunEvent:
        return RunEvent(
            run_id=RunId(row["run_id"]),
            sequence=int(row["sequence"]),
            event_id=EventId(row["event_id"]),
            event_type=EventType(row["event_type"]),
            message=row["message"],
            run_status=RunStatus(row["run_status"]) if row["run_status"] else None,
            task_id=TaskId(row["task_id"]) if row["task_id"] else None,
            task_status=TaskStatus(row["task_status"]) if row["task_status"] else None,
            artifact_id=ArtifactId(row["artifact_id"]) if row["artifact_id"] else None,
            approval_id=ApprovalId(row["approval_id"]) if row["approval_id"] else None,
            payload=json_loads(row["payload_json"]),
            created_at=parse_datetime(row["created_at"]) or utc_now(),
        )

    def _artifact_from_row(self, row: sqlite3.Row) -> Artifact:
        return Artifact(
            artifact_id=ArtifactId(row["artifact_id"]),
            run_id=RunId(row["run_id"]),
            task_id=TaskId(row["task_id"]) if row["task_id"] else None,
            artifact_type=ArtifactType(row["artifact_type"]),
            label=row["label"],
            uri=row["uri"],
            created_at=parse_datetime(row["created_at"]) or utc_now(),
        )

    def _approval_request_from_row(self, row: sqlite3.Row) -> ApprovalRequest:
        payload = json_loads(row["command_argv_json"])
        command_argv = payload.get("command_argv", [])
        if not isinstance(command_argv, list):
            command_argv = []
        return ApprovalRequest(
            approval_id=ApprovalId(row["approval_id"]),
            run_id=RunId(row["run_id"]),
            task_id=TaskId(row["task_id"]) if row["task_id"] else None,
            patch_id=ArtifactId(row["patch_id"]) if row["patch_id"] else None,
            reason=row["reason"],
            command_argv=tuple(str(item) for item in command_argv),
            created_at=parse_datetime(row["created_at"]) or utc_now(),
            expires_at=parse_datetime(row["expires_at"]),
        )


__all__ = ["SQLiteExecutionRuntimeRepository"]
