"""Agent core service exports."""

from .model_client import FakeModelClient, LLMProviderModelClient, ModelClient
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

_BOOTSTRAP_EXPORTS = {
    "AgentCoreModelConfig",
    "LocalAgentRunnerConfig",
    "LocalAgentRunnerStack",
    "build_local_agent_runner_stack",
    "build_local_agent_runner_stack_from_env",
    "build_model_client",
    "find_local_agent_runner_config_file",
    "load_local_agent_runner_config_from_file",
    "load_local_agent_runner_config_from_env",
    "resolve_local_agent_runner_config",
}

__all__ = [
    "AgentAction",
    "AgentActionType",
    "AgentCoreModelConfig",
    "AgentContextBudget",
    "AgentCoreCoordinator",
    "AgentCoreService",
    "AgentFailure",
    "FakeModelClient",
    "LLMProviderModelClient",
    "LocalAgentRunnerConfig",
    "LocalAgentRunnerStack",
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
    "build_local_agent_runner_stack",
    "build_local_agent_runner_stack_from_env",
    "build_model_client",
    "find_local_agent_runner_config_file",
    "load_local_agent_runner_config_from_file",
    "load_local_agent_runner_config_from_env",
    "resolve_local_agent_runner_config",
]


def __getattr__(name: str):
    if name == "LocalAgentCoreService":
        from .local import LocalAgentCoreService

        return LocalAgentCoreService
    if name in _BOOTSTRAP_EXPORTS:
        from . import bootstrap as _bootstrap

        return getattr(_bootstrap, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
