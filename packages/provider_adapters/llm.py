from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Mapping, Sequence

from packages.shared_types import LLMMessage, LLMResponse


class LLMProvider(ABC):
    """Adapter for model providers used by the agent core."""

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[LLMMessage],
        model: str,
        *,
        response_format: Mapping[str, object] | None = None,
    ) -> LLMResponse:
        """Return a single non-streaming completion."""

    @abstractmethod
    async def stream(self, messages: Sequence[LLMMessage], model: str) -> AsyncIterator[str]:
        """Yield streaming model output chunks."""

    @abstractmethod
    async def count_tokens(self, messages: Sequence[LLMMessage], model: str) -> int:
        """Estimate token usage for the supplied messages."""
