"""Execution runtime service exports."""

from .service import ExecutionRuntimeService

__all__ = [
    "ExecutionRuntimeService",
    "LocalExecutionRuntimeService",
    "SQLiteExecutionRuntimeRepository",
]


def __getattr__(name: str):
    if name == "LocalExecutionRuntimeService":
        from .local import LocalExecutionRuntimeService

        return LocalExecutionRuntimeService
    if name == "SQLiteExecutionRuntimeRepository":
        from .sqlite_store import SQLiteExecutionRuntimeRepository

        return SQLiteExecutionRuntimeRepository
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
