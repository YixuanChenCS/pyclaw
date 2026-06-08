"""Repo intelligence service interface."""

from .service import RepoIntelligenceService

__all__ = ["LocalRepoIntelligenceService", "RepoIntelligenceService"]


def __getattr__(name: str):
    if name == "LocalRepoIntelligenceService":
        from .local import LocalRepoIntelligenceService

        return LocalRepoIntelligenceService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
