from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from packages.shared_types import (
    FileSummary,
    ImpactAnalysis,
    RepoContextRequest,
    RepoContextResult,
    SymbolMatch,
    WatchSubscription,
    WorkspaceRef,
)


class RepoIntelligenceService(ABC):
    """Repo indexing, search, and impact-analysis service."""

    @abstractmethod
    async def inspect_workspace(self, workspace: WorkspaceRef) -> WorkspaceRef:
        """Validate workspace metadata and repo state."""

    @abstractmethod
    async def build_context(self, request: RepoContextRequest) -> RepoContextResult:
        """Build repository context for a run."""

    @abstractmethod
    async def refresh_index(self, workspace: WorkspaceRef, changed_files: Sequence[str]) -> None:
        """Refresh repo-intelligence state after file or branch changes."""

    @abstractmethod
    async def summarize_files(self, workspace: WorkspaceRef, files: Sequence[str]) -> Sequence[FileSummary]:
        """Summarize the requested files for UI or agent context."""

    @abstractmethod
    async def search_symbols(self, workspace: WorkspaceRef, query: str) -> Sequence[SymbolMatch]:
        """Find matching symbols or structural identifiers."""

    @abstractmethod
    async def analyze_impact(self, workspace: WorkspaceRef, files: Sequence[str]) -> ImpactAnalysis:
        """Analyze downstream impact of proposed changes."""

    @abstractmethod
    async def watch_workspace(self, workspace: WorkspaceRef) -> WatchSubscription:
        """Return a watch descriptor for future repo-change subscriptions."""
