from __future__ import annotations

import unittest
from unittest.mock import patch

from packages.provider_adapters import LiteLLMProvider
from packages.shared_types import LLMMessage


class _ChoiceMessage:
    def __init__(self, *, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, *, content: str | None, finish_reason: str | None = "stop") -> None:
        self.message = _ChoiceMessage(content=content)
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, *, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Response:
    def __init__(
        self,
        *,
        content: str | None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        finish_reason: str | None = "stop",
    ) -> None:
        self.choices = [_Choice(content=content, finish_reason=finish_reason)]
        self.usage = _Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


class _StreamChunk:
    def __init__(self, content: str | None) -> None:
        self.choices = [{"delta": {"content": content}}]


class _AsyncStream:
    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class TestLiteLLMProvider(unittest.IsolatedAsyncioTestCase):
    async def test_complete_maps_litellm_response_to_shared_contract(self):
        # Verifies that the concrete provider converts a litellm completion into the shared LLMResponse contract.
        # This catches adapter bugs that would drop finish_reason, usage, or message content before agent_core sees them.
        # The mapped response is correct because it preserves the exact model name and token counts returned by litellm.
        provider = LiteLLMProvider()
        messages = (LLMMessage(role="user", content="Return JSON only."),)

        async def fake_acompletion(*, model, messages, stream, response_format=None):
            self.assertEqual(model, "openai/gpt-4o-mini")
            self.assertEqual(messages, [{"role": "user", "content": "Return JSON only."}])
            self.assertFalse(stream)
            self.assertIsNone(response_format)
            return _Response(content='{"ok":true}', prompt_tokens=11, completion_tokens=7)

        with patch(
            "packages.provider_adapters.litellm_provider.litellm.acompletion",
            side_effect=fake_acompletion,
        ) as mocked_acompletion:
            response = await provider.complete(messages, "openai/gpt-4o-mini")

        self.assertEqual(mocked_acompletion.call_count, 1)
        self.assertEqual(response.provider, "litellm")
        self.assertEqual(response.model, "openai/gpt-4o-mini")
        self.assertEqual(response.content, '{"ok":true}')
        self.assertEqual(response.usage.input_tokens, 11)
        self.assertEqual(response.usage.output_tokens, 7)
        self.assertEqual(response.finish_reason, "stop")

    async def test_complete_forwards_response_format_to_litellm(self):
        # Verifies that JSON-mode requests survive the provider boundary and reach the exact litellm call.
        # This catches the bug where complete_json wiring exists in agent_core but the provider silently drops response_format.
        # Forwarding is correct because provider-backed JSON planning should ask the underlying model API for JSON output explicitly.
        provider = LiteLLMProvider()

        async def fake_acompletion(**kwargs):
            self.assertEqual(kwargs["response_format"], {"type": "json_object"})
            return _Response(content='{"ok":true}')

        with patch(
            "packages.provider_adapters.litellm_provider.litellm.acompletion",
            side_effect=fake_acompletion,
        ):
            await provider.complete(
                (LLMMessage(role="user", content="Return JSON only."),),
                "openai/gpt-4o-mini",
                response_format={"type": "json_object"},
            )

    async def test_complete_rejects_missing_message_content(self):
        # Verifies that the provider fails loudly when litellm returns a choice without usable message content.
        # This catches permissive behavior that would convert malformed completions into empty agent responses.
        # Rejection is correct because the shared LLMResponse contract requires concrete text content.
        provider = LiteLLMProvider()

        async def fake_acompletion(*, model, messages, stream, response_format=None):
            return _Response(content=None)

        with patch(
            "packages.provider_adapters.litellm_provider.litellm.acompletion",
            side_effect=fake_acompletion,
        ):
            with self.assertRaises(ValueError) as context:
                await provider.complete((LLMMessage(role="user", content="Hi"),), "openai/gpt-4o-mini")

        self.assertIn("message content", str(context.exception).lower())

    async def test_stream_yields_only_non_empty_delta_content(self):
        # Verifies that the streaming path emits only actual text deltas and skips empty housekeeping chunks.
        # This catches stream adapters that would surface None or empty fragments as fake model output.
        # The yielded sequence is correct because only the non-empty delta chunks contain user-visible text.
        provider = LiteLLMProvider()

        async def fake_acompletion(*, model, messages, stream):
            self.assertTrue(stream)
            return _AsyncStream(
                [
                    _StreamChunk(None),
                    _StreamChunk("hello"),
                    _StreamChunk(" world"),
                    _StreamChunk(""),
                ]
            )

        with patch(
            "packages.provider_adapters.litellm_provider.litellm.acompletion",
            side_effect=fake_acompletion,
        ):
            chunks = [
                chunk
                async for chunk in provider.stream(
                    (LLMMessage(role="user", content="Stream"),),
                    "openai/gpt-4o-mini",
                )
            ]

        self.assertEqual(chunks, ["hello", " world"])

    async def test_count_tokens_calls_exact_litellm_symbol(self):
        # Verifies that token counting goes through the exact litellm token_counter symbol used by the implementation.
        # This catches weak tests that patch the wrong symbol and would still pass even if the real dependency call changed.
        # The expected count is correct because the patched token_counter returns the authoritative integer for this request.
        provider = LiteLLMProvider()

        with patch(
            "packages.provider_adapters.litellm_provider.litellm.token_counter",
            return_value=23,
        ) as mocked_token_counter:
            token_count = await provider.count_tokens(
                (LLMMessage(role="user", content="Count this"),),
                "openai/gpt-4o-mini",
            )

        self.assertEqual(token_count, 23)
        mocked_token_counter.assert_called_once_with(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "Count this"}],
        )


if __name__ == "__main__":
    unittest.main()
