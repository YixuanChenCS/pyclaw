from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os

from packages.shared_types import LLMResponse, TokenUsage
from services.agent_core import (
    AgentCoreModelConfig,
    LocalAgentRunnerConfig,
    build_local_agent_runner_stack,
    build_local_agent_runner_stack_from_env,
    build_model_client,
    load_local_agent_runner_config_from_file,
    load_local_agent_runner_config_from_env,
    resolve_local_agent_runner_config,
)
from services.agent_core.model_client import LLMProviderModelClient
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository


class _DummyProvider:
    async def complete(self, messages, model, *, response_format=None):
        return LLMResponse(
            provider="dummy",
            model=model,
            content="{}",
            usage=TokenUsage(),
            finish_reason="stop",
        )

    async def stream(self, messages, model):
        if False:
            yield messages, model

    async def count_tokens(self, messages, model):
        return 0


class _RepoStore:
    async def get_workspace(self, workspace_id):
        return None


class TestAgentCoreBootstrap(unittest.TestCase):
    def test_load_local_agent_runner_config_from_env_reads_explicit_values(self):
        # Verifies that the bootstrap config layer reads the runner settings from explicit env keys.
        # This catches hidden hand-wiring where the runtime path or model selection still ignores configuration input.
        # The parsed values are correct because each field maps directly from one environment variable into the typed config object.
        config = load_local_agent_runner_config_from_env(
            {
                "AGENT_CORE_MODEL_PROVIDER": "litellm",
                "AGENT_CORE_MODEL": "openai/gpt-4.1-mini",
                "AGENT_CORE_SYSTEM_PROMPT": "JSON only please.",
                "OPENAI_API_KEY": "env-openai-key",
                "EXECUTION_RUNTIME_DB_PATH": "/tmp/agent-runtime.sqlite3",
                "EXECUTION_RUNTIME_STREAM_POLL_INTERVAL": "0.2",
            }
        )

        self.assertEqual(config.model.provider, "litellm")
        self.assertEqual(config.model.model, "openai/gpt-4.1-mini")
        self.assertEqual(config.model.system_prompt, "JSON only please.")
        self.assertEqual(dict(config.api_keys), {"openai": "env-openai-key"})
        self.assertEqual(config.runtime_db_path, "/tmp/agent-runtime.sqlite3")
        self.assertEqual(config.stream_poll_interval, 0.2)
        self.assertEqual(config.fallback_test_command, ())

    def test_load_local_agent_runner_config_from_env_reads_fallback_test_command(self):
        config = load_local_agent_runner_config_from_env(
            {
                "AGENT_CORE_FALLBACK_TEST_COMMAND": "python -m pytest tests/unit -k agent_core",
            }
        )

        self.assertEqual(
            config.fallback_test_command,
            ("python", "-m", "pytest", "tests/unit", "-k", "agent_core"),
        )

    def test_load_local_agent_runner_config_from_env_rejects_invalid_poll_interval(self):
        # Verifies that malformed bootstrap numeric config fails loudly instead of silently falling back.
        # This catches permissive config parsing that would mask a broken deployment configuration and run with unintended timing.
        # Rejection is correct because the stream poll interval is a typed float in the runner config contract.
        with self.assertRaises(ValueError) as context:
            load_local_agent_runner_config_from_env(
                {"EXECUTION_RUNTIME_STREAM_POLL_INTERVAL": "fast"}
            )

        self.assertIn("must be a float", str(context.exception))

    def test_build_model_client_selects_litellm_provider_from_config(self):
        # Verifies that model-client construction goes through the configured provider selector rather than ad-hoc manual wiring.
        # This catches regressions where the bootstrap layer stops honoring the model provider config and hardcodes a test fake.
        # The built adapter is correct because litellm is the configured provider and the model/system prompt must be preserved verbatim.
        dummy_provider = _DummyProvider()

        with patch(
            "services.agent_core.bootstrap.LiteLLMProvider",
            return_value=dummy_provider,
        ) as mocked_provider_ctor:
            client = build_model_client(
                AgentCoreModelConfig(
                    provider="litellm",
                    model="openai/gpt-4o-mini",
                    system_prompt="Return JSON only.",
                )
            )

        self.assertIsInstance(client, LLMProviderModelClient)
        self.assertIs(client.provider, dummy_provider)
        self.assertEqual(client.model, "openai/gpt-4o-mini")
        self.assertEqual(client.system_prompt, "Return JSON only.")
        mocked_provider_ctor.assert_called_once_with()

    def test_build_model_client_rejects_unknown_provider(self):
        # Verifies that unsupported provider names are rejected at bootstrap time instead of failing much later at runtime.
        # This catches weak configuration handling that would let an unknown provider string leak into execution setup.
        # Rejection is correct because the current bootstrap layer only knows how to construct the litellm-backed adapter.
        with self.assertRaises(ValueError) as context:
            build_model_client(
                AgentCoreModelConfig(
                    provider="unknown-provider",
                    model="some-model",
                )
            )

        self.assertIn("unsupported agent-core model provider", str(context.exception).lower())

    def test_load_local_agent_runner_config_from_file_reads_agent_core_section(self):
        # Verifies that project config files can supply agent_core settings through a dedicated YAML section.
        # This catches the bug where .pyclaw.conf.yml would be discovered but the new agent_core path ignored its scoped config.
        # The loaded values are correct because the YAML file explicitly sets model, API key, and runtime fields under their config sections.
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".pyclaw.conf.yml"
            config_path.write_text(
                """
agent_core:
  model:
    provider: litellm
    model: openai/gpt-4.1
    system_prompt: Config prompt.
  api_keys:
    openai: file-openai-key
execution_runtime:
  db_path: /tmp/from-file.sqlite3
  stream_poll_interval: 0.25
""".strip(),
                encoding="utf-8",
            )

            config = load_local_agent_runner_config_from_file(config_path)

        self.assertEqual(config.model.provider, "litellm")
        self.assertEqual(config.model.model, "openai/gpt-4.1")
        self.assertEqual(config.model.system_prompt, "Config prompt.")
        self.assertEqual(dict(config.api_keys), {"openai": "file-openai-key"})
        self.assertEqual(config.runtime_db_path, "/tmp/from-file.sqlite3")
        self.assertEqual(config.stream_poll_interval, 0.25)
        self.assertEqual(config.fallback_test_command, ())

    def test_load_local_agent_runner_config_from_file_reads_verification_fallback_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".pyclaw.conf.yml"
            config_path.write_text(
                """
agent_core:
  verification:
    fallback_test_command:
      - python
      - -m
      - pytest
      - tests/unit
execution_runtime:
  db_path: /tmp/from-file.sqlite3
""".strip(),
                encoding="utf-8",
            )

            config = load_local_agent_runner_config_from_file(config_path)

        self.assertEqual(
            config.fallback_test_command,
            ("python", "-m", "pytest", "tests/unit"),
        )

    def test_resolve_local_agent_runner_config_applies_cli_over_file_over_env_over_default(self):
        # Verifies the full precedence chain: CLI overrides project config, project config overrides environment, and environment overrides defaults.
        # This catches ambiguous bootstrap behavior where a lower-priority source could silently clobber a user-supplied CLI setting.
        # The final values are correct because each asserted field comes from the highest-priority source that provided it.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".agent_core.yml").write_text(
                """
model:
  provider: litellm
  model: openai/gpt-4.1-mini
  system_prompt: File prompt.
api_keys:
  openai: file-openai-key
""".strip(),
                encoding="utf-8",
            )

            config = resolve_local_agent_runner_config(
                workspace_root=root,
                env={
                    "AGENT_CORE_MODEL": "openai/gpt-4o-mini",
                    "OPENAI_API_KEY": "env-openai-key",
                },
                cli_overrides={
                    "provider": "litellm",
                    "model": "openai/gpt-5-mini",
                    "system_prompt": "CLI prompt.",
                    "api_keys": ("openai=cli-openai-key",),
                    "openai_api_key": None,
                    "anthropic_api_key": None,
                    "openrouter_api_key": None,
                    "runtime_db_path": None,
                    "stream_poll_interval": None,
                    "fallback_test_command": "python -m pytest tests/cli",
                },
            )

        self.assertEqual(config.model.model, "openai/gpt-5-mini")
        self.assertEqual(config.model.system_prompt, "CLI prompt.")
        self.assertEqual(dict(config.api_keys), {"openai": "cli-openai-key"})
        self.assertEqual(
            config.fallback_test_command,
            ("python", "-m", "pytest", "tests/cli"),
        )

    def test_resolve_local_agent_runner_config_uses_file_over_env_when_cli_missing(self):
        # Verifies that project config beats environment variables when no CLI override is supplied.
        # This catches precedence regressions where env would unexpectedly override repository-local agent configuration.
        # The file-backed values are correct because the project config is the highest-priority configured source in this scenario.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".agent_core.yml").write_text(
                """
model:
  model: openai/gpt-4.1-mini
api_keys:
  openai: file-openai-key
""".strip(),
                encoding="utf-8",
            )

            config = resolve_local_agent_runner_config(
                workspace_root=root,
                env={
                    "AGENT_CORE_MODEL": "openai/gpt-4o-mini",
                    "OPENAI_API_KEY": "env-openai-key",
                },
            )

        self.assertEqual(config.model.model, "openai/gpt-4.1-mini")
        self.assertEqual(dict(config.api_keys), {"openai": "file-openai-key"})

    def test_build_local_agent_runner_stack_wires_shared_repository_and_services(self):
        # Verifies that the bootstrap factory creates one coherent local stack with shared runtime/session persistence.
        # This catches the bug where the coordinator, runtime, and agent_core would each get different repositories and drift apart.
        # The wiring is correct because the stack must share one SQLite repository for runtime state and AgentSession snapshots.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "runtime.sqlite3")
            dummy_provider = _DummyProvider()

            with patch(
                "services.agent_core.bootstrap.LiteLLMProvider",
                return_value=dummy_provider,
            ):
                stack = build_local_agent_runner_stack(
                    config=LocalAgentRunnerConfig(
                        model=AgentCoreModelConfig(
                            provider="litellm",
                            model="openai/gpt-4o-mini",
                        ),
                        runtime_db_path=db_path,
                        stream_poll_interval=0.01,
                    ),
                    repo_store=_RepoStore(),
                )

        self.assertIsInstance(stack.repository, SQLiteExecutionRuntimeRepository)
        self.assertIsInstance(stack.execution_runtime, LocalExecutionRuntimeService)
        self.assertEqual(stack.repository.db_path, Path(db_path))
        self.assertIs(stack.execution_runtime.repository, stack.repository)
        self.assertIs(stack.coordinator._session_store, stack.repository)
        self.assertIs(stack.coordinator._execution_runtime, stack.execution_runtime)
        self.assertIs(stack.coordinator._agent_core, stack.agent_core)
        self.assertIs(stack.model_client.provider, dummy_provider)
        self.assertEqual(
            stack.agent_core._fallback_test_command,
            (),
        )

    def test_build_local_agent_runner_stack_passes_fallback_test_command_to_agent_core(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "runtime.sqlite3")
            dummy_provider = _DummyProvider()

            with patch(
                "services.agent_core.bootstrap.LiteLLMProvider",
                return_value=dummy_provider,
            ):
                stack = build_local_agent_runner_stack(
                    config=LocalAgentRunnerConfig(
                        model=AgentCoreModelConfig(
                            provider="litellm",
                            model="openai/gpt-4o-mini",
                        ),
                        runtime_db_path=db_path,
                        fallback_test_command=("python", "-m", "pytest", "tests/unit"),
                    ),
                    repo_store=_RepoStore(),
                )

        self.assertEqual(
            stack.agent_core._fallback_test_command,
            ("python", "-m", "pytest", "tests/unit"),
        )

    def test_build_local_agent_runner_stack_applies_provider_api_keys_to_environment(self):
        # Verifies that resolved provider-scoped API keys are pushed into the process environment before the real provider runs.
        # This catches the bug where CLI/config precedence resolves correctly on paper but litellm still reads stale environment variables at call time.
        # The applied environment value is correct because the final resolved config selected this exact OpenAI API key.
        with tempfile.TemporaryDirectory() as tmpdir:
            dummy_provider = _DummyProvider()
            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "services.agent_core.bootstrap.LiteLLMProvider",
                    return_value=dummy_provider,
                ):
                    build_local_agent_runner_stack(
                        config=LocalAgentRunnerConfig(
                            model=AgentCoreModelConfig(
                                provider="litellm",
                                model="openai/gpt-4o-mini",
                            ),
                            api_keys={"openai": "resolved-openai-key"},
                            runtime_db_path=str(Path(tmpdir) / "runtime.sqlite3"),
                        ),
                        repo_store=_RepoStore(),
                    )

                self.assertEqual(os.environ["OPENAI_API_KEY"], "resolved-openai-key")

    def test_build_local_agent_runner_stack_from_env_uses_env_selected_model(self):
        # Verifies that the env-based stack factory carries model selection through to the constructed model client.
        # This catches the gap where env parsing might work but the factory still instantiate a default hardcoded model.
        # The selected model is correct because the env config explicitly requested it for the bootstrap path.
        with tempfile.TemporaryDirectory() as tmpdir:
            dummy_provider = _DummyProvider()

            with patch(
                "services.agent_core.bootstrap.LiteLLMProvider",
                return_value=dummy_provider,
            ):
                stack = build_local_agent_runner_stack_from_env(
                    repo_store=_RepoStore(),
                    env={
                        "AGENT_CORE_MODEL_PROVIDER": "litellm",
                        "AGENT_CORE_MODEL": "openai/gpt-4.1-mini",
                        "EXECUTION_RUNTIME_DB_PATH": str(Path(tmpdir) / "runtime.sqlite3"),
                    },
                )

        self.assertEqual(stack.config.model.model, "openai/gpt-4.1-mini")
        self.assertEqual(stack.model_client.model, "openai/gpt-4.1-mini")
        self.assertIs(stack.model_client.provider, dummy_provider)


if __name__ == "__main__":
    unittest.main()
