from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from packages.shared_types import LLMMessage
from packages.shared_types.protocols import LLMProvider


ModelResponse = str | Mapping[str, object]
JSON_RESPONSE_FORMAT: Mapping[str, object] = {"type": "json_object"}


class ModelClient(Protocol):
    """Headless model-client boundary for deterministic planning."""

    async def complete_json(self, prompt: str) -> ModelResponse:
        """Return a JSON string or structured JSON-like mapping."""


@dataclass(slots=True)
class FakeModelClient:
    """Deterministic fake client that replays pre-seeded responses."""

    responses: list[ModelResponse] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    async def complete_json(self, prompt: str) -> ModelResponse:
        self.prompts.append(prompt)
        if not self.responses:
            raise RuntimeError("FakeModelClient has no remaining responses")
        return self.responses.pop(0)


@dataclass(slots=True)
class LLMProviderModelClient:
    """ModelClient adapter backed by the shared LLMProvider protocol."""

    provider: LLMProvider
    model: str
    system_prompt: str | None = None
    response_format: Mapping[str, object] | None = field(
        default_factory=lambda: dict(JSON_RESPONSE_FORMAT)
    )
    prompts: list[str] = field(default_factory=list)

    async def complete_json(self, prompt: str) -> ModelResponse:
        self.prompts.append(prompt)

        messages: list[LLMMessage] = []
        if self.system_prompt is not None and self.system_prompt.strip():
            messages.append(
                LLMMessage(
                    role="system",
                    content=self.system_prompt,
                )
            )
        messages.append(
            LLMMessage(
                role="user",
                content=prompt,
            )
        )

        response = await self.provider.complete(
            tuple(messages),
            self.model,
            response_format=self.response_format,
        )
        if not response.content or not response.content.strip():
            raise ValueError("LLMProvider returned empty completion content")
        return response.content
