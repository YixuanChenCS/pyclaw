from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Mapping, Sequence

import litellm

from packages.shared_types import LLMMessage, LLMResponse, TokenUsage


def _message_payload(messages: Sequence[LLMMessage]) -> list[dict[str, str]]:
    return [{"role": message.role, "content": message.content} for message in messages]


def _get_mapping_value(obj: object, key: str) -> object | None:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_choice(response: object) -> object:
    choices = _get_mapping_value(response, "choices")
    if not isinstance(choices, Sequence) or not choices:
        raise ValueError("LiteLLM response did not include any choices")
    return choices[0]


def _extract_message_content(choice: object) -> str:
    message = _get_mapping_value(choice, "message")
    content = _get_mapping_value(message, "content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LiteLLM response did not include non-empty message content")
    return content


def _extract_finish_reason(choice: object) -> str | None:
    finish_reason = _get_mapping_value(choice, "finish_reason")
    if finish_reason is None:
        return None
    if not isinstance(finish_reason, str):
        raise ValueError("LiteLLM finish_reason must be a string when present")
    return finish_reason


def _extract_usage(response: object) -> TokenUsage:
    usage = _get_mapping_value(response, "usage")
    if usage is None:
        return TokenUsage()

    prompt_tokens = _get_mapping_value(usage, "prompt_tokens")
    completion_tokens = _get_mapping_value(usage, "completion_tokens")
    if prompt_tokens is None and completion_tokens is None:
        return TokenUsage()
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        raise ValueError("LiteLLM usage tokens must be integers when present")
    return TokenUsage(
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
    )


def _extract_stream_delta_text(chunk: object) -> str:
    choice = _extract_choice(chunk)
    delta = _get_mapping_value(choice, "delta")
    if delta is None:
        return ""
    content = _get_mapping_value(delta, "content")
    if content is None:
        return ""
    if not isinstance(content, str):
        raise ValueError("LiteLLM stream delta content must be a string when present")
    return content


@dataclass(slots=True)
class LiteLLMProvider:
    """Concrete LLMProvider backed by litellm."""

    provider_name: str = "litellm"

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        model: str,
        *,
        response_format: Mapping[str, object] | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, object] = {
            "model": model,
            "messages": _message_payload(messages),
            "stream": False,
        }
        if response_format is not None:
            kwargs["response_format"] = dict(response_format)
        response = await litellm.acompletion(**kwargs)
        choice = _extract_choice(response)
        return LLMResponse(
            provider=self.provider_name,
            model=model,
            content=_extract_message_content(choice),
            usage=_extract_usage(response),
            finish_reason=_extract_finish_reason(choice),
        )

    async def stream(self, messages: Sequence[LLMMessage], model: str) -> AsyncIterator[str]:
        stream = await litellm.acompletion(
            model=model,
            messages=_message_payload(messages),
            stream=True,
        )
        async for chunk in stream:
            text = _extract_stream_delta_text(chunk)
            if text:
                yield text

    async def count_tokens(self, messages: Sequence[LLMMessage], model: str) -> int:
        token_count = litellm.token_counter(
            model=model,
            messages=_message_payload(messages),
        )
        if not isinstance(token_count, int):
            raise ValueError("LiteLLM token counter must return an integer")
        return token_count
