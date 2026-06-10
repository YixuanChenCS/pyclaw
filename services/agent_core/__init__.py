"""Agent core service exports."""

from .model_client import FakeModelClient, ModelClient
from .models import (
    AgentAction,
    AgentActionType,
    AgentContextBudget,
    AgentFailure,
    AgentPlan,
    AgentSession,
    AgentStep,
    LoopGuardResult,
    PatchReview,
    RunSummary,
)
from .service import AgentCoreService

__all__ = [
    "AgentAction",
    "AgentActionType",
    "AgentContextBudget",
    "AgentCoreService",
    "AgentFailure",
    "FakeModelClient",
    "AgentPlan",
    "AgentSession",
    "AgentStep",
    "LocalAgentCoreService",
    "LoopGuardResult",
    "ModelClient",
    "PatchReview",
    "RunSummary",
]


def __getattr__(name: str):
    if name == "LocalAgentCoreService":
        from .local import LocalAgentCoreService

        return LocalAgentCoreService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
