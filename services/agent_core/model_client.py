from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol


ModelResponse = str | Mapping[str, object]


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
