from __future__ import annotations

import unittest

from packages.shared_types import LLMMessage, LLMResponse, RepoContextResult, TokenUsage
from services.agent_core import LLMProviderModelClient, LocalAgentCoreService
from packages.shared_types import new_run_id, new_workspace_id


class _RecordingProvider:
    def __init__(self, *, response: LLMResponse) -> None:
        self.response = response
        self.calls: list[tuple[tuple[LLMMessage, ...], str, object]] = []

    async def complete(self, messages, model, *, response_format=None):
        self.calls.append((tuple(messages), model, response_format))
        return self.response

    async def stream(self, messages, model):
        if False:
            yield messages, model

    async def count_tokens(self, messages, model):
        return 0


class TestLLMProviderModelClient(unittest.IsolatedAsyncioTestCase):
    async def test_complete_json_wraps_system_and_user_messages(self):
        # Verifies that the adapter turns a raw prompt into the exact provider message shape agent_core needs.
        # This catches integration bugs where the prompt is sent with the wrong role ordering or wrong model name.
        # The expected messages are correct because the adapter contract is one optional system message followed by one user prompt.
        provider = _RecordingProvider(
            response=LLMResponse(
                provider="test-provider",
                model="test-model",
                content='{"goal":"Plan","steps":[{"kind":"complete","description":"Done"}]}',
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                finish_reason="stop",
            )
        )
        client = LLMProviderModelClient(
            provider=provider,
            model="test-model",
            system_prompt="Return JSON only.",
        )

        response = await client.complete_json("Plan this task.")

        self.assertEqual(
            response,
            '{"goal":"Plan","steps":[{"kind":"complete","description":"Done"}]}',
        )
        self.assertEqual(client.prompts, ["Plan this task."])
        self.assertEqual(len(provider.calls), 1)
        messages, called_model, response_format = provider.calls[0]
        self.assertEqual(called_model, "test-model")
        self.assertEqual(response_format, {"type": "json_object"})
        self.assertEqual(
            messages,
            (
                LLMMessage(role="system", content="Return JSON only."),
                LLMMessage(role="user", content="Plan this task."),
            ),
        )

    async def test_complete_json_rejects_blank_provider_content(self):
        # Verifies that the adapter fails loudly when the provider returns no usable content.
        # This catches weak adapter behavior that would pass empty completions into plan parsing and hide the real failure boundary.
        # Rejection is correct because agent_core cannot build structured JSON from blank model output.
        provider = _RecordingProvider(
            response=LLMResponse(
                provider="test-provider",
                model="test-model",
                content="   ",
            )
        )
        client = LLMProviderModelClient(provider=provider, model="test-model")

        with self.assertRaises(ValueError) as context:
            await client.complete_json("Plan this task.")

        self.assertIn("empty completion content", str(context.exception).lower())

    async def test_create_plan_accepts_provider_backed_model_client(self):
        # Verifies that LocalAgentCoreService can plan through the real provider-backed adapter, not only FakeModelClient.
        # This catches wiring bugs where the new adapter type satisfies the protocol on paper but fails in the actual create_plan path.
        # The resulting plan is correct because the provider returns a valid planner JSON payload with one inspect step.
        provider = _RecordingProvider(
            response=LLMResponse(
                provider="test-provider",
                model="test-model",
                content=(
                    '{"goal":"Inspect the service","steps":['
                    '{"kind":"inspect","description":"Inspect services/agent_core/local.py","target_files":["services/agent_core/local.py"]}'
                    ']}'
                ),
            )
        )
        service = LocalAgentCoreService(
            model_client=LLMProviderModelClient(provider=provider, model="test-model")
        )
        run_id = new_run_id()
        workspace_id = new_workspace_id()
        session = service.create_session(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request="Plan through the provider adapter",
            repo_context=RepoContextResult(
                workspace_id=workspace_id,
                run_id=run_id,
                repo_map="services/\n  agent_core/\n",
            ),
        )

        plan = await service.create_plan(session)

        self.assertEqual(plan.goal, "Inspect the service")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].step_id, "step_1")
        self.assertEqual(plan.steps[0].kind, "inspect")
        self.assertEqual(plan.steps[0].target_files, ("services/agent_core/local.py",))


if __name__ == "__main__":
    unittest.main()
