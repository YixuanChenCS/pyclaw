from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .ids import WorkspaceId
from .models import SerializableModel, utc_now


@dataclass(frozen=True, slots=True, kw_only=True)
class LockLease(SerializableModel):
    workspace_id: WorkspaceId
    owner_id: str
    lease_id: str
    acquired_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None


class WorkspaceLockManager(Protocol):
    async def acquire(
        self,
        workspace_id: WorkspaceId,
        *,
        owner_id: str,
        lease_id: str,
        ttl_seconds: int | None = None,
    ) -> LockLease:
        """Acquire or renew exclusive access to a workspace."""

    async def release(self, lease: LockLease) -> None:
        """Release a previously acquired lease."""

    async def heartbeat(self, lease: LockLease) -> LockLease:
        """Extend an active lease."""


__all__ = ["LockLease", "WorkspaceLockManager"]
