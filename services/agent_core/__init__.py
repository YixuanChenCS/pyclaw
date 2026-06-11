"""Agent core service exports."""

from .model_client import FakeModelClient, ModelClient
from .models import (
    AgentAction,
    AgentActionType,
    AgentContextBudget,
    AgentFailure,
    AgentPlan,
    AgentRunOutcome,
    AgentSession,
    AgentSessionPhase,
    AgentStep,
    LoopGuardResult,
    PatchReview,
    RunSummary,
)
from .runner import AgentCoreCoordinator
from .service import AgentCoreService

__all__ = [
    "AgentAction",
    "AgentActionType",
    "AgentContextBudget",
    "AgentCoreCoordinator",
    "AgentCoreService",
    "AgentFailure",
    "FakeModelClient",
    "AgentPlan",
    "AgentRunOutcome",
    "AgentSession",
    "AgentSessionPhase",
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
