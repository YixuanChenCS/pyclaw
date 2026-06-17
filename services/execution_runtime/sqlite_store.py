from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Iterator, Mapping, Sequence

from packages.shared_types import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalId,
    Artifact,
    ArtifactId,
    ArtifactType,
    EntityNotFoundError,
    ErrorCode,
    ErrorCodeContractError,
    EventId,
    EventType,
    JSONValue,
    RecoveryOption,
    RecoveryState,
    RecoveryStatus,
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
from services.agent_core.session_store import (
    AGENT_SESSION_SCHEMA_VERSION,
    deserialize_agent_session_json,
    serialize_agent_session,
)

from .events import json_dumps, json_loads, parse_datetime, run_status_event_type, serialize_datetime
from .state_machine import TERMINAL_RUN_STATUSES, validate_run_transition

if TYPE_CHECKING:
    from services.agent_core.models import AgentSession

_RECOVERY_SIDE_EFFECT_EVENT_TYPES = frozenset(
    {
        EventType.COMMAND_STARTED,
        EventType.PATCH_APPLIED,
    }
)


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

    async def create_run_idempotent(
        self,
        run: Run,
        *,
        events: Sequence[RunEvent] = (),
        request_fingerprint: str,
    ) -> bool:
        """Create a run once, returning False when the same request already exists."""
        return await asyncio.to_thread(
            self._create_run_idempotent_sync,
            run,
            tuple(events),
            request_fingerprint,
        )

    async def get_run(self, run_id: RunId | str) -> Run | None:
        return await asyncio.to_thread(self._get_run_sync, RunId(str(run_id)))

    async def list_runs(
        self,
        workspace_id: WorkspaceId | str | None = None,
        *,
        session_id: SessionId | str | None = None,
        status: RunStatus | str | None = None,
    ) -> tuple[Run, ...]:
        typed_workspace_id = None if workspace_id is None else WorkspaceId(str(workspace_id))
        typed_session_id = None if session_id is None else SessionId(str(session_id))
        typed_status = (
            None
            if status is None
            else status
            if isinstance(status, RunStatus)
            else RunStatus(status)
        )
        return await asyncio.to_thread(
            self._list_runs_sync,
            typed_workspace_id,
            typed_session_id,
            typed_status,
        )

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

    async def get_event_sequence(
        self,
        run_id: RunId | str,
        event_id: EventId | str,
    ) -> int | None:
        return await asyncio.to_thread(
            self._get_event_sequence_sync,
            RunId(str(run_id)),
            EventId(str(event_id)),
        )

    async def event_sequence_exists(
        self,
        run_id: RunId | str,
        sequence: int,
    ) -> bool:
        return await asyncio.to_thread(
            self._event_sequence_exists_sync,
            RunId(str(run_id)),
            sequence,
        )

    async def get_health_snapshot(self) -> dict[str, object]:
        return await asyncio.to_thread(self._get_health_snapshot_sync)

    async def claim_next_run(
        self,
        worker_id: str,
        lease_seconds: int,
    ) -> Run | None:
        return await asyncio.to_thread(self._claim_next_run_sync, worker_id, lease_seconds)

    async def claim_run(
        self,
        run_id: RunId | str,
        worker_id: str,
        lease_seconds: int,
    ) -> Run | None:
        return await asyncio.to_thread(
            self._claim_run_sync,
            RunId(str(run_id)),
            worker_id,
            lease_seconds,
        )

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

    async def get_artifact(self, artifact_id: ArtifactId | str) -> Artifact | None:
        return await asyncio.to_thread(self._get_artifact_sync, ArtifactId(str(artifact_id)))

    async def create_approval_request(self, request: ApprovalRequest) -> RunEvent:
        return await asyncio.to_thread(self._create_approval_request_sync, request)

    async def list_approval_requests(self, run_id: RunId | str) -> tuple[ApprovalRequest, ...]:
        return await asyncio.to_thread(self._list_approval_requests_sync, RunId(str(run_id)))

    async def list_approvals(
        self,
        *,
        run_id: RunId | str | None = None,
    ) -> tuple[ApprovalRecord, ...]:
        typed_run_id = RunId(str(run_id)) if run_id is not None else None
        return await asyncio.to_thread(self._list_approvals_sync, typed_run_id)

    async def get_approval(self, approval_id: ApprovalId | str) -> ApprovalRecord | None:
        return await asyncio.to_thread(self._get_approval_sync, ApprovalId(str(approval_id)))

    async def update_approval_decision(self, decision: ApprovalDecision) -> None:
        await asyncio.to_thread(self._update_approval_decision_sync, decision)

    async def save_patch_snapshot(
        self,
        *,
        run_id: RunId | str,
        task_id: TaskId | str,
        relative_path: str,
        existed_before: bool,
        content: str | None,
    ) -> None:
        await asyncio.to_thread(
            self._save_patch_snapshot_sync,
            RunId(str(run_id)),
            TaskId(str(task_id)),
            relative_path,
            existed_before,
            content,
        )

    async def list_patch_snapshots(
        self,
        run_id: RunId | str,
        task_id: TaskId | str,
    ) -> tuple[dict[str, object], ...]:
        return await asyncio.to_thread(
            self._list_patch_snapshots_sync,
            RunId(str(run_id)),
            TaskId(str(task_id)),
        )

    async def upsert_recovery_status(self, recovery: RecoveryStatus) -> None:
        await asyncio.to_thread(self._upsert_recovery_status_sync, recovery)

    async def get_recovery_status(self, run_id: RunId | str) -> RecoveryStatus | None:
        return await asyncio.to_thread(self._get_recovery_status_sync, RunId(str(run_id)))

    async def clear_recovery_status(self, run_id: RunId | str) -> None:
        await asyncio.to_thread(self._clear_recovery_status_sync, RunId(str(run_id)))

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

                CREATE TABLE IF NOT EXISTS run_idempotency (
                    run_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
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

                CREATE TABLE IF NOT EXISTS agent_sessions (
                    run_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    session_json TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS patch_snapshots (
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    existed_before INTEGER NOT NULL,
                    content TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, task_id, relative_path)
                );

                CREATE TABLE IF NOT EXISTS recovery_states (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    recovery_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    recovery_options_json TEXT NOT NULL,
                    rollback_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runs_status_created
                    ON runs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_runs_status_lease
                    ON runs(status, lease_expires_at);
                CREATE INDEX IF NOT EXISTS idx_run_events_sequence
                    ON run_events(run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_workspace
                    ON agent_sessions(workspace_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_patch_snapshots_task
                    ON patch_snapshots(run_id, task_id);
                """
            )
        finally:
            connection.close()

    async def save_agent_session(self, session: AgentSession) -> None:
        await asyncio.to_thread(self._save_agent_session_sync, session)

    async def load_agent_session(self, run_id: RunId | str) -> AgentSession | None:
        return await asyncio.to_thread(self._load_agent_session_sync, RunId(str(run_id)))

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

    def _create_run_idempotent_sync(
        self,
        run: Run,
        events: Sequence[RunEvent],
        request_fingerprint: str,
    ) -> bool:
        with self._transaction("IMMEDIATE") as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (str(run.run_id),),
            ).fetchone()
            if row is not None:
                existing = self._run_from_row(row)
                fingerprint_row = connection.execute(
                    "SELECT request_fingerprint FROM run_idempotency WHERE run_id = ?",
                    (str(run.run_id),),
                ).fetchone()
                if fingerprint_row is not None:
                    if fingerprint_row["request_fingerprint"] == request_fingerprint:
                        return False
                    raise ErrorCodeContractError(
                        ErrorCode.INVALID_REQUEST,
                        f"Run id {run.run_id} is already used by a different request.",
                        details={"run_id": str(run.run_id)},
                    )
                if (
                    existing.workspace_id == run.workspace_id
                    and existing.session_id == run.session_id
                    and existing.prompt == run.prompt
                ):
                    return False
                raise ErrorCodeContractError(
                    ErrorCode.INVALID_REQUEST,
                    f"Run id {run.run_id} is already used by a different request.",
                    details={"run_id": str(run.run_id)},
                )

            self._insert_run(connection, run)
            connection.execute(
                "INSERT INTO run_idempotency (run_id, request_fingerprint) VALUES (?, ?)",
                (str(run.run_id), request_fingerprint),
            )
            for event in events:
                self._append_event_with_sequence_in_tx(connection, run.run_id, event)
            return True

    def _get_event_sequence_sync(self, run_id: RunId, event_id: EventId) -> int | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT sequence FROM run_events WHERE run_id = ? AND event_id = ?",
                (str(run_id), str(event_id)),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else int(row["sequence"])

    def _event_sequence_exists_sync(self, run_id: RunId, sequence: int) -> bool:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT 1 FROM run_events WHERE run_id = ? AND sequence = ?",
                (str(run_id), sequence),
            ).fetchone()
        finally:
            connection.close()
        return row is not None

    def _get_health_snapshot_sync(self) -> dict[str, object]:
        serialized_now = serialize_datetime(utc_now())
        connection = self._connect()
        try:
            connection.execute("SELECT 1").fetchone()
            status_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM runs GROUP BY status"
            ).fetchall()
            artifact_count = connection.execute(
                "SELECT COUNT(*) AS count FROM artifacts"
            ).fetchone()
            queue_depth = connection.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE status = ?",
                (RunStatus.QUEUED.value,),
            ).fetchone()
            stale_run_count = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM runs
                WHERE status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                """,
                (RunStatus.RUNNING.value, serialized_now),
            ).fetchone()
            needs_recovery_count = connection.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE status = ?",
                (RunStatus.NEEDS_RECOVERY.value,),
            ).fetchone()
            active_lease_count = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM runs
                WHERE status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at >= ?
                """,
                (RunStatus.RUNNING.value, serialized_now),
            ).fetchone()
        finally:
            connection.close()
        return {
            "db": "ready",
            "queue": {
                str(row["status"]): int(row["count"])
                for row in status_rows
            },
            "queue_depth": int(queue_depth["count"]),
            "stale_run_count": int(stale_run_count["count"]),
            "needs_recovery_count": int(needs_recovery_count["count"]),
            "active_lease_count": int(active_lease_count["count"]),
            "artifact_store": "ready",
            "artifact_count": int(artifact_count["count"]),
        }

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
        serialized_now = serialize_datetime(now)
        with self._transaction("IMMEDIATE") as connection:
            row = connection.execute(
                """
                SELECT queued.*
                FROM runs AS queued
                WHERE queued.status = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM runs AS active
                      WHERE active.workspace_id = queued.workspace_id
                        AND active.status = ?
                        AND active.lease_expires_at IS NOT NULL
                        AND active.lease_expires_at >= ?
                  )
                ORDER BY queued.created_at ASC, queued.run_id ASC
                LIMIT 1
                """,
                (
                    RunStatus.QUEUED.value,
                    RunStatus.RUNNING.value,
                    serialized_now,
                ),
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

    def _claim_run_sync(self, run_id: RunId, worker_id: str, lease_seconds: int) -> Run | None:
        now = utc_now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        serialized_now = serialize_datetime(now)
        with self._transaction("IMMEDIATE") as connection:
            row = connection.execute(
                """
                SELECT *
                FROM runs
                WHERE run_id = ?
                """,
                (str(run_id),),
            ).fetchone()
            if row is None:
                return None

            current = self._run_from_row(row)
            if current.status == RunStatus.RUNNING:
                return current
            if current.status != RunStatus.QUEUED:
                return None

            conflicting_active = connection.execute(
                """
                SELECT 1
                FROM runs AS active
                WHERE active.workspace_id = ?
                  AND active.run_id != ?
                  AND active.status = ?
                  AND active.lease_expires_at IS NOT NULL
                  AND active.lease_expires_at >= ?
                LIMIT 1
                """,
                (
                    str(current.workspace_id),
                    str(current.run_id),
                    RunStatus.RUNNING.value,
                    serialized_now,
                ),
            ).fetchone()
            if conflicting_active is not None:
                return None

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
        serialized_now = serialize_datetime(now)
        with self._transaction("IMMEDIATE") as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                ORDER BY lease_expires_at ASC, run_id ASC
                """,
                (RunStatus.RUNNING.value, serialized_now),
            ).fetchall()
            for row in rows:
                current = self._run_from_row(row)
                side_effect_event_types = self._side_effect_events_since_last_start(
                    connection,
                    current.run_id,
                )
                if side_effect_event_types:
                    recovery = self._build_stale_recovery_status(
                        connection,
                        run_id=current.run_id,
                        reason="Manual recovery required because the worker lease expired after side effects.",
                        created_at=now,
                    )
                    recovered = replace(
                        current,
                        status=RunStatus.NEEDS_RECOVERY,
                        worker_id=None,
                        lease_expires_at=None,
                        last_heartbeat_at=None,
                        updated_at=now,
                    )
                    self._update_run_row(connection, recovered)
                    self._upsert_recovery_status_in_tx(connection, recovery)
                    self._append_event_with_sequence_in_tx(
                        connection,
                        recovered.run_id,
                        build_run_event(
                            run_id=recovered.run_id,
                            event_type=EventType.RUN_NEEDS_RECOVERY,
                            message=recovery.reason,
                            run_status=RunStatus.NEEDS_RECOVERY,
                            task_id=recovery.task_id,
                            payload={
                                "kind": "manual_recovery",
                                "recovered_from_status": current.status.value,
                                "previous_worker_id": current.worker_id,
                                "attempt": current.attempt,
                                "recovery_state": recovery.recovery_state.value,
                                "recovery_options": [option.value for option in recovery.recovery_options],
                            },
                        ),
                    )
                    self._append_event_with_sequence_in_tx(
                        connection,
                        recovered.run_id,
                        build_run_event(
                            run_id=recovered.run_id,
                            event_type=EventType.AGENT_MESSAGE,
                            message=(
                                "Worker lease expired after side effects; run requires manual recovery."
                            ),
                            run_status=RunStatus.NEEDS_RECOVERY,
                            task_id=recovery.task_id,
                            payload={
                                "kind": "manual_recovery_required",
                                "side_effect_event_types": sorted(side_effect_event_types),
                                "recovery_state": recovery.recovery_state.value,
                            },
                        ),
                    )
                else:
                    recovered = replace(
                        current,
                        status=RunStatus.QUEUED,
                        worker_id=None,
                        lease_expires_at=None,
                        last_heartbeat_at=None,
                        updated_at=now,
                    )
                    self._update_run_row(connection, recovered)
                    self._append_event_with_sequence_in_tx(
                        connection,
                        recovered.run_id,
                        build_run_status_event(
                            recovered,
                            EventType.RUN_QUEUED,
                            message="Recovered stale run after lease expiry before side effects.",
                            payload={
                                "recovered_from_status": current.status.value,
                                "previous_worker_id": current.worker_id,
                                "attempt": current.attempt,
                                "recovery_strategy": "requeued",
                            },
                        ),
                    )
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

    def _get_artifact_sync(self, artifact_id: ArtifactId) -> Artifact | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE artifact_id = ?
                """,
                (str(artifact_id),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return self._artifact_from_row(row)

    def _list_runs_sync(
        self,
        workspace_id: WorkspaceId | None,
        session_id: SessionId | None,
        status: RunStatus | None,
    ) -> tuple[Run, ...]:
        connection = self._connect()
        try:
            clauses: list[str] = []
            values: list[str] = []
            if workspace_id is not None:
                clauses.append("workspace_id = ?")
                values.append(str(workspace_id))
            if session_id is not None:
                clauses.append("session_id = ?")
                values.append(str(session_id))
            if status is not None:
                clauses.append("status = ?")
                values.append(status.value)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"""
                SELECT * FROM runs
                {where}
                ORDER BY created_at DESC, run_id DESC
                """,
                tuple(values),
            ).fetchall()
        finally:
            connection.close()

        return tuple(self._run_from_row(row) for row in rows)

    def _create_approval_request_sync(self, request: ApprovalRequest) -> RunEvent:
        with self._transaction("IMMEDIATE") as connection:
            row = self._select_run_for_update(connection, request.run_id)
            current = self._run_from_row(row)
            validate_run_transition(current.status, RunStatus.WAITING_FOR_APPROVAL)

            self._insert_approval_request(connection, request)

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

    def _list_approvals_sync(self, run_id: RunId | None) -> tuple[ApprovalRecord, ...]:
        connection = self._connect()
        try:
            if run_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM approvals
                    ORDER BY created_at ASC, approval_id ASC
                    """
                ).fetchall()
            else:
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
        return tuple(self._approval_record_from_row(row) for row in rows)

    def _get_approval_sync(self, approval_id: ApprovalId) -> ApprovalRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM approvals
                WHERE approval_id = ?
                """,
                (str(approval_id),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return self._approval_record_from_row(row)

    def _update_approval_decision_sync(self, decision: ApprovalDecision) -> None:
        with self._transaction("IMMEDIATE") as connection:
            approval_row = connection.execute(
                """
                SELECT *
                FROM approvals
                WHERE approval_id = ?
                """,
                (str(decision.approval_id),),
            ).fetchone()
            if approval_row is None:
                raise EntityNotFoundError("approval", str(decision.approval_id))

            if str(approval_row["run_id"]) != str(decision.run_id):
                raise ValueError("ApprovalDecision.run_id must match the persisted approval run_id")

            if approval_row["approved"] is not None:
                approved_value = bool(int(approval_row["approved"]))
                if (
                    approved_value == decision.approved
                    and approval_row["reviewer"] == decision.reviewer
                    and approval_row["comment"] == decision.comment
                ):
                    return
                raise ErrorCodeContractError(
                    ErrorCode.APPROVAL_ALREADY_RESOLVED,
                    f"Approval {decision.approval_id} has already been decided and cannot be overwritten",
                    details={"approval_id": str(decision.approval_id)},
                )

            expires_at = parse_datetime(approval_row["expires_at"])
            decided_at = parse_datetime(serialize_datetime(decision.decided_at))
            if expires_at is not None and decided_at is not None and decided_at >= expires_at:
                raise ErrorCodeContractError(
                    ErrorCode.APPROVAL_EXPIRED,
                    f"Approval {decision.approval_id} expired at {expires_at.isoformat()}.",
                    details={
                        "approval_id": str(decision.approval_id),
                        "expires_at": expires_at.isoformat(),
                    },
                )

            connection.execute(
                """
                UPDATE approvals
                SET approved = ?, decided_at = ?, reviewer = ?, comment = ?
                WHERE approval_id = ?
                """,
                (
                    1 if decision.approved else 0,
                    serialize_datetime(decision.decided_at),
                    decision.reviewer,
                    decision.comment,
                    str(decision.approval_id),
                ),
            )

            run = self._run_from_row(self._select_run_for_update(connection, decision.run_id))
            self._append_event_with_sequence_in_tx(
                connection,
                run.run_id,
                build_run_event(
                    run_id=run.run_id,
                    event_type=EventType.APPROVAL_RESOLVED,
                    run_status=run.status,
                    approval_id=decision.approval_id,
                    task_id=TaskId(str(approval_row["task_id"])) if approval_row["task_id"] is not None else None,
                    message="Approval granted" if decision.approved else "Approval denied",
                    payload={
                        "approved": decision.approved,
                        "reviewer": decision.reviewer,
                        "comment": decision.comment,
                    },
                ),
            )

    def _save_agent_session_sync(self, session: AgentSession) -> None:
        now = utc_now()
        serialized = serialize_agent_session(session)
        with self._transaction("IMMEDIATE") as connection:
            existing = connection.execute(
                """
                SELECT revision, created_at
                FROM agent_sessions
                WHERE run_id = ?
                """,
                (str(session.run_id),),
            ).fetchone()

            if existing is None:
                connection.execute(
                    """
                    INSERT INTO agent_sessions (
                        run_id, workspace_id, session_json, schema_version, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(session.run_id),
                        str(session.workspace_id),
                        serialized,
                        AGENT_SESSION_SCHEMA_VERSION,
                        1,
                        serialize_datetime(now),
                        serialize_datetime(now),
                    ),
                )
                return

            connection.execute(
                """
                UPDATE agent_sessions
                SET workspace_id = ?, session_json = ?, schema_version = ?, revision = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    str(session.workspace_id),
                    serialized,
                    AGENT_SESSION_SCHEMA_VERSION,
                    int(existing["revision"]) + 1,
                    serialize_datetime(now),
                    str(session.run_id),
                ),
            )

    def _load_agent_session_sync(self, run_id: RunId) -> AgentSession | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT session_json, schema_version
                FROM agent_sessions
                WHERE run_id = ?
                """,
                (str(run_id),),
            ).fetchone()
        finally:
            connection.close()

        if row is None:
            return None

        return deserialize_agent_session_json(
            row["session_json"],
            schema_version=int(row["schema_version"]),
        )

    def _save_patch_snapshot_sync(
        self,
        run_id: RunId,
        task_id: TaskId,
        relative_path: str,
        existed_before: bool,
        content: str | None,
    ) -> None:
        with self._transaction("IMMEDIATE") as connection:
            connection.execute(
                """
                INSERT INTO patch_snapshots (
                    run_id, task_id, relative_path, existed_before, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, task_id, relative_path) DO UPDATE SET
                    existed_before = excluded.existed_before,
                    content = excluded.content
                """,
                (
                    str(run_id),
                    str(task_id),
                    relative_path,
                    1 if existed_before else 0,
                    content,
                    serialize_datetime(utc_now()),
                ),
            )

    def _list_patch_snapshots_sync(
        self,
        run_id: RunId,
        task_id: TaskId,
    ) -> tuple[dict[str, object], ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT relative_path, existed_before, content, created_at
                FROM patch_snapshots
                WHERE run_id = ? AND task_id = ?
                ORDER BY relative_path ASC
                """,
                (str(run_id), str(task_id)),
            ).fetchall()
        finally:
            connection.close()

        return tuple(
            {
                "relative_path": str(row["relative_path"]),
                "existed_before": bool(int(row["existed_before"])),
                "content": row["content"],
                "created_at": parse_datetime(row["created_at"]) or utc_now(),
            }
            for row in rows
        )

    def _upsert_recovery_status_sync(self, recovery: RecoveryStatus) -> None:
        with self._transaction("IMMEDIATE") as connection:
            self._upsert_recovery_status_in_tx(connection, recovery)

    def _get_recovery_status_sync(self, run_id: RunId) -> RecoveryStatus | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT *
                FROM recovery_states
                WHERE run_id = ?
                """,
                (str(run_id),),
            ).fetchone()
        finally:
            connection.close()

        if row is None:
            return None
        return self._recovery_status_from_row(row)

    def _clear_recovery_status_sync(self, run_id: RunId) -> None:
        with self._transaction("IMMEDIATE") as connection:
            connection.execute(
                "DELETE FROM recovery_states WHERE run_id = ?",
                (str(run_id),),
            )

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

    def _insert_approval_request(
        self,
        connection: sqlite3.Connection,
        request: ApprovalRequest,
    ) -> None:
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

    def _upsert_recovery_status_in_tx(
        self,
        connection: sqlite3.Connection,
        recovery: RecoveryStatus,
    ) -> None:
        connection.execute(
            """
            INSERT INTO recovery_states (
                run_id, task_id, recovery_state, reason, recovery_options_json,
                rollback_task_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                task_id = excluded.task_id,
                recovery_state = excluded.recovery_state,
                reason = excluded.reason,
                recovery_options_json = excluded.recovery_options_json,
                rollback_task_id = excluded.rollback_task_id,
                updated_at = excluded.updated_at
            """,
            (
                str(recovery.run_id),
                str(recovery.task_id) if recovery.task_id is not None else None,
                recovery.recovery_state.value,
                recovery.reason,
                json_dumps({"recovery_options": [item.value for item in recovery.recovery_options]}),
                str(recovery.rollback_task_id) if recovery.rollback_task_id is not None else None,
                serialize_datetime(recovery.created_at),
                serialize_datetime(recovery.updated_at),
            ),
        )

    def _build_stale_recovery_status(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: RunId,
        reason: str,
        created_at: datetime,
    ) -> RecoveryStatus:
        rollback_task_id = self._latest_snapshot_task_id(connection, run_id)
        if rollback_task_id is not None:
            state = RecoveryState.ROLLBACK_AVAILABLE
            options = (
                RecoveryOption.ROLLBACK_IF_AVAILABLE,
                RecoveryOption.REVIEW_MANUALLY,
                RecoveryOption.ABORT,
            )
        else:
            state = RecoveryState.NEEDS_RECOVERY
            options = (
                RecoveryOption.REVIEW_MANUALLY,
                RecoveryOption.ABORT,
            )
        return RecoveryStatus(
            run_id=run_id,
            task_id=rollback_task_id,
            recovery_state=state,
            reason=reason,
            recovery_options=options,
            rollback_task_id=rollback_task_id,
            created_at=created_at,
            updated_at=created_at,
        )

    def _latest_snapshot_task_id(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
    ) -> TaskId | None:
        row = connection.execute(
            """
            SELECT task_id
            FROM patch_snapshots
            WHERE run_id = ?
            ORDER BY created_at DESC, task_id DESC
            LIMIT 1
            """,
            (str(run_id),),
        ).fetchone()
        if row is None or row["task_id"] is None:
            return None
        return TaskId(str(row["task_id"]))

    def _side_effect_events_since_last_start(
        self,
        connection: sqlite3.Connection,
        run_id: RunId,
    ) -> set[str]:
        last_started_row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) AS max_sequence
            FROM run_events
            WHERE run_id = ? AND event_type = ?
            """,
            (str(run_id), EventType.RUN_STARTED.value),
        ).fetchone()
        last_started_sequence = int(last_started_row["max_sequence"])
        rows = connection.execute(
            """
            SELECT event_type, payload_json
            FROM run_events
            WHERE run_id = ? AND sequence > ?
            ORDER BY sequence ASC
            """,
            (str(run_id), last_started_sequence),
        ).fetchall()
        side_effects: set[str] = set()
        for row in rows:
            event_type = row["event_type"]
            if event_type in {item.value for item in _RECOVERY_SIDE_EFFECT_EVENT_TYPES}:
                side_effects.add(event_type)
                continue
            if event_type != EventType.AGENT_MESSAGE.value:
                continue
            payload = json_loads(row["payload_json"])
            if isinstance(payload, Mapping) and payload.get("kind") == "patch.started":
                side_effects.add("patch.started")
        return side_effects

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

    def _approval_record_from_row(self, row: sqlite3.Row) -> ApprovalRecord:
        payload = json_loads(row["command_argv_json"])
        raw_command_argv = payload.get("command_argv", [])
        if not isinstance(raw_command_argv, list):
            raw_command_argv = []
        command_argv = tuple(str(item) for item in raw_command_argv)
        expires_at = parse_datetime(row["expires_at"])
        decided_at = parse_datetime(row["decided_at"])
        created_at = parse_datetime(row["created_at"]) or utc_now()
        approved_value = row["approved"]
        approved = None if approved_value is None else bool(int(approved_value))
        kind = "patch" if row["patch_id"] else "command" if command_argv else "generic"
        status = self._approval_status_from_row(
            approved=approved,
            expires_at=expires_at,
        )
        return ApprovalRecord(
            approval_id=ApprovalId(row["approval_id"]),
            run_id=RunId(row["run_id"]),
            status=status,
            kind=kind,
            reason=row["reason"],
            task_id=TaskId(row["task_id"]) if row["task_id"] else None,
            patch_id=ArtifactId(row["patch_id"]) if row["patch_id"] else None,
            command_argv=command_argv,
            approved=approved,
            created_at=created_at,
            updated_at=decided_at,
            decided_at=decided_at,
            expires_at=expires_at,
            reviewer=row["reviewer"],
            comment=row["comment"],
        )

    def _approval_status_from_row(
        self,
        *,
        approved: bool | None,
        expires_at,
    ) -> str:
        if approved is True:
            return "approved"
        if approved is False:
            return "rejected"
        if expires_at is not None and expires_at <= utc_now():
            return "expired"
        return "pending"

    def _recovery_status_from_row(self, row: sqlite3.Row) -> RecoveryStatus:
        payload = json_loads(row["recovery_options_json"])
        raw_options = payload.get("recovery_options", [])
        if not isinstance(raw_options, list):
            raw_options = []
        return RecoveryStatus(
            run_id=RunId(row["run_id"]),
            task_id=TaskId(row["task_id"]) if row["task_id"] else None,
            recovery_state=RecoveryState(row["recovery_state"]),
            reason=row["reason"],
            recovery_options=tuple(RecoveryOption(str(item)) for item in raw_options),
            rollback_task_id=TaskId(row["rollback_task_id"]) if row["rollback_task_id"] else None,
            created_at=parse_datetime(row["created_at"]) or utc_now(),
            updated_at=parse_datetime(row["updated_at"]) or utc_now(),
        )


__all__ = ["SQLiteExecutionRuntimeRepository"]
