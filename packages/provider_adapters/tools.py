from __future__ import annotations

from abc import ABC, abstractmethod

from packages.shared_types import CommandRequest, CommandResult, DeploymentRequest, DeploymentResult, PatchProposal


class ShellAdapter(ABC):
    """Adapter for shell command execution backends."""

    @abstractmethod
    async def execute(self, request: CommandRequest) -> CommandResult:
        """Execute the requested command."""


class PatchAdapter(ABC):
    """Adapter for patch application backends."""

    @abstractmethod
    async def apply(self, proposal: PatchProposal) -> str:
        """Apply a patch and return the resulting artifact identifier."""


class DeploymentAdapter(ABC):
    """Adapter for deployment and post-deploy workflows."""

    @abstractmethod
    async def deploy(self, request: DeploymentRequest) -> DeploymentResult:
        """Run the requested deployment."""
