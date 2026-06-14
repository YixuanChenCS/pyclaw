from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from packages.provider_adapters import LiteLLMProvider
from packages.shared_types import RepoStore
from services.execution_runtime import LocalExecutionRuntimeService, SQLiteExecutionRuntimeRepository
from services.repo_intelligence.service import RepoIntelligenceService

from .local import LocalAgentCoreService
from .model_client import LLMProviderModelClient, ModelClient
from .runner import AgentCoreCoordinator

DEFAULT_AGENT_CORE_SYSTEM_PROMPT = "Return JSON only."
DEFAULT_PROJECT_CONFIG_FILENAMES = (".agent_core.yml", ".pyclaw.conf.yml")
PROVIDER_API_KEY_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentCoreModelConfig:
    provider: str = "litellm"
    model: str = "openai/gpt-4o-mini"
    system_prompt: str = DEFAULT_AGENT_CORE_SYSTEM_PROMPT


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalAgentRunnerConfig:
    model: AgentCoreModelConfig = field(default_factory=AgentCoreModelConfig)
    api_keys: Mapping[str, str] = field(default_factory=dict)
    runtime_db_path: str = ".execution_runtime/runtime.sqlite3"
    stream_poll_interval: float = 0.05


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalAgentRunnerStack:
    config: LocalAgentRunnerConfig
    model_client: ModelClient
    repository: SQLiteExecutionRuntimeRepository
    agent_core: LocalAgentCoreService
    execution_runtime: LocalExecutionRuntimeService
    coordinator: AgentCoreCoordinator


def load_local_agent_runner_config_from_env(
    env: Mapping[str, str] | None = None,
) -> LocalAgentRunnerConfig:
    source = env if env is not None else os.environ
    return _config_from_payload(
        {
            "model": {
                "provider": source.get("AGENT_CORE_MODEL_PROVIDER", "litellm"),
                "model": source.get("AGENT_CORE_MODEL", "openai/gpt-4o-mini"),
                "system_prompt": source.get(
                    "AGENT_CORE_SYSTEM_PROMPT",
                    DEFAULT_AGENT_CORE_SYSTEM_PROMPT,
                ),
            },
            "api_keys": {
                provider: source[env_var]
                for provider, env_var in PROVIDER_API_KEY_ENV_VARS.items()
                if source.get(env_var)
            },
            "runtime_db_path": source.get(
                "EXECUTION_RUNTIME_DB_PATH",
                ".execution_runtime/runtime.sqlite3",
            ),
            "stream_poll_interval": source.get(
                "EXECUTION_RUNTIME_STREAM_POLL_INTERVAL",
                0.05,
            ),
        }
    )


def load_local_agent_runner_config_from_file(path: str | Path) -> LocalAgentRunnerConfig:
    config_path = Path(path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"Agent-core config file must contain a YAML mapping: {config_path}")

    scoped = payload.get("agent_core", payload)
    if not isinstance(scoped, Mapping):
        raise ValueError(f"agent_core config must be a mapping in {config_path}")

    runtime_payload = payload.get("execution_runtime", {})
    if runtime_payload is None:
        runtime_payload = {}
    if not isinstance(runtime_payload, Mapping):
        raise ValueError(f"execution_runtime config must be a mapping in {config_path}")

    model_payload = scoped.get("model", {})
    if model_payload is None:
        model_payload = {}
    if not isinstance(model_payload, Mapping):
        raise ValueError(f"agent_core.model must be a mapping in {config_path}")

    return _config_from_payload(
        {
            "model": {
                "provider": model_payload.get("provider", scoped.get("provider")),
                "model": model_payload.get("model", scoped.get("model")),
                "system_prompt": model_payload.get(
                    "system_prompt",
                    scoped.get("system_prompt"),
                ),
            },
            "api_keys": scoped.get("api_keys", {}),
            "runtime_db_path": runtime_payload.get(
                "db_path",
                scoped.get("runtime_db_path"),
            ),
            "stream_poll_interval": runtime_payload.get(
                "stream_poll_interval",
                scoped.get("stream_poll_interval"),
            ),
        }
    )


def find_local_agent_runner_config_file(
    *,
    workspace_root: str | Path | None = None,
    explicit_path: str | Path | None = None,
) -> Path | None:
    if explicit_path is not None:
        return Path(explicit_path)
    if workspace_root is None:
        return None

    root = Path(workspace_root)
    for name in DEFAULT_PROJECT_CONFIG_FILENAMES:
        candidate = root / name
        if candidate.exists():
            return candidate
    return None


def resolve_local_agent_runner_config(
    *,
    workspace_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> LocalAgentRunnerConfig:
    resolved_payload: dict[str, Any] = {}
    resolved_payload = _merge_config_payloads(resolved_payload, _payload_from_env(env))

    discovered_path = find_local_agent_runner_config_file(
        workspace_root=workspace_root,
        explicit_path=config_path,
    )
    if discovered_path is not None and discovered_path.exists():
        resolved_payload = _merge_config_payloads(
            resolved_payload,
            _payload_from_file(discovered_path),
        )
    elif config_path is not None:
        raise FileNotFoundError(f"Agent-core config file was not found: {config_path}")

    if cli_overrides:
        resolved_payload = _merge_config_payloads(
            resolved_payload,
            _payload_from_cli_overrides(cli_overrides),
        )

    return _config_from_payload(resolved_payload)


def build_model_client(config: AgentCoreModelConfig) -> ModelClient:
    provider = config.provider.strip().lower()
    if provider == "litellm":
        return LLMProviderModelClient(
            provider=LiteLLMProvider(),
            model=config.model,
            system_prompt=config.system_prompt,
        )
    raise ValueError(f"Unsupported agent-core model provider: {config.provider!r}")


def build_local_agent_runner_stack(
    *,
    config: LocalAgentRunnerConfig,
    repo_store: RepoStore,
    repo_intelligence: RepoIntelligenceService | None = None,
) -> LocalAgentRunnerStack:
    _apply_provider_api_keys(config.api_keys)
    repository = SQLiteExecutionRuntimeRepository(Path(config.runtime_db_path))
    model_client = build_model_client(config.model)
    agent_core = LocalAgentCoreService(
        model_client=model_client,
        session_store=repository,
    )
    execution_runtime = LocalExecutionRuntimeService(
        repository=repository,
        repo_store=repo_store,
        stream_poll_interval=config.stream_poll_interval,
    )
    coordinator = AgentCoreCoordinator(
        agent_core=agent_core,
        execution_runtime=execution_runtime,
        session_store=repository,
        repo_intelligence=repo_intelligence,
        repo_store=repo_store,
    )
    return LocalAgentRunnerStack(
        config=config,
        model_client=model_client,
        repository=repository,
        agent_core=agent_core,
        execution_runtime=execution_runtime,
        coordinator=coordinator,
    )


def build_local_agent_runner_stack_from_env(
    *,
    repo_store: RepoStore,
    env: Mapping[str, str] | None = None,
) -> LocalAgentRunnerStack:
    return build_local_agent_runner_stack(
        config=load_local_agent_runner_config_from_env(env),
        repo_store=repo_store,
    )


def _config_from_cli_overrides(cli_overrides: Mapping[str, Any]) -> LocalAgentRunnerConfig:
    return _config_from_payload(_payload_from_cli_overrides(cli_overrides))


def _config_from_payload(payload: Mapping[str, Any]) -> LocalAgentRunnerConfig:
    model_payload = payload.get("model", {})
    if model_payload is None:
        model_payload = {}
    if not isinstance(model_payload, Mapping):
        raise ValueError("model config must be a mapping")

    api_keys_payload = payload.get("api_keys", {})
    if api_keys_payload is None:
        api_keys_payload = {}
    if not isinstance(api_keys_payload, Mapping):
        raise ValueError("api_keys config must be a mapping")

    runtime_db_path = payload.get("runtime_db_path", ".execution_runtime/runtime.sqlite3")
    if runtime_db_path is None:
        runtime_db_path = ".execution_runtime/runtime.sqlite3"

    stream_poll_interval = payload.get("stream_poll_interval", 0.05)
    if stream_poll_interval is None:
        poll_interval = 0.05
    else:
        try:
            poll_interval = float(stream_poll_interval)
        except ValueError as exc:
            raise ValueError("EXECUTION_RUNTIME_STREAM_POLL_INTERVAL must be a float") from exc

    return LocalAgentRunnerConfig(
        model=AgentCoreModelConfig(
            provider=str(model_payload.get("provider", "litellm")),
            model=str(model_payload.get("model", "openai/gpt-4o-mini")),
            system_prompt=str(
                model_payload.get("system_prompt", DEFAULT_AGENT_CORE_SYSTEM_PROMPT)
            ),
        ),
        api_keys=_normalize_api_keys(api_keys_payload),
        runtime_db_path=str(runtime_db_path),
        stream_poll_interval=poll_interval,
    )


def _normalize_api_keys(api_keys_payload: Mapping[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for provider, raw_key in api_keys_payload.items():
        provider_name = str(provider).strip().lower()
        key = str(raw_key).strip()
        if not provider_name:
            raise ValueError("API key provider names must be non-empty")
        if not key:
            raise ValueError(f"API key for provider {provider_name!r} must be non-empty")
        normalized[provider_name] = key
    return normalized


def _parse_cli_api_keys(
    api_key_entries: Sequence[str],
    *,
    openai_api_key: str | None = None,
    anthropic_api_key: str | None = None,
    openrouter_api_key: str | None = None,
) -> dict[str, str]:
    api_keys: dict[str, str] = {}
    for entry in api_key_entries:
        provider, separator, key = entry.partition("=")
        if not separator:
            raise ValueError(f"CLI --api-key entries must use provider=key form: {entry!r}")
        provider_name = provider.strip().lower()
        key_value = key.strip()
        if not provider_name or not key_value:
            raise ValueError(f"CLI --api-key entries must use provider=key form: {entry!r}")
        api_keys[provider_name] = key_value

    if openai_api_key is not None:
        api_keys["openai"] = openai_api_key.strip()
    if anthropic_api_key is not None:
        api_keys["anthropic"] = anthropic_api_key.strip()
    if openrouter_api_key is not None:
        api_keys["openrouter"] = openrouter_api_key.strip()
    return api_keys


def _apply_provider_api_keys(api_keys: Mapping[str, str]) -> None:
    for provider, key in api_keys.items():
        env_var = PROVIDER_API_KEY_ENV_VARS.get(provider.strip().lower())
        if env_var is None:
            continue
        os.environ[env_var] = key


def _payload_from_env(env: Mapping[str, str] | None) -> dict[str, Any]:
    source = env if env is not None else os.environ
    payload: dict[str, Any] = {}

    model_payload = {
        key: value
        for key, value in (
            ("provider", source.get("AGENT_CORE_MODEL_PROVIDER")),
            ("model", source.get("AGENT_CORE_MODEL")),
            ("system_prompt", source.get("AGENT_CORE_SYSTEM_PROMPT")),
        )
        if value is not None
    }
    if model_payload:
        payload["model"] = model_payload

    api_keys = {
        provider: source[env_var]
        for provider, env_var in PROVIDER_API_KEY_ENV_VARS.items()
        if source.get(env_var)
    }
    if api_keys:
        payload["api_keys"] = api_keys

    if source.get("EXECUTION_RUNTIME_DB_PATH") is not None:
        payload["runtime_db_path"] = source["EXECUTION_RUNTIME_DB_PATH"]
    if source.get("EXECUTION_RUNTIME_STREAM_POLL_INTERVAL") is not None:
        payload["stream_poll_interval"] = source["EXECUTION_RUNTIME_STREAM_POLL_INTERVAL"]

    return payload


def _payload_from_file(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"Agent-core config file must contain a YAML mapping: {config_path}")

    scoped = payload.get("agent_core", payload)
    if not isinstance(scoped, Mapping):
        raise ValueError(f"agent_core config must be a mapping in {config_path}")

    runtime_payload = payload.get("execution_runtime", {})
    if runtime_payload is None:
        runtime_payload = {}
    if not isinstance(runtime_payload, Mapping):
        raise ValueError(f"execution_runtime config must be a mapping in {config_path}")

    model_payload = scoped.get("model", {})
    if model_payload is None:
        model_payload = {}
    if not isinstance(model_payload, Mapping):
        raise ValueError(f"agent_core.model must be a mapping in {config_path}")

    result: dict[str, Any] = {}
    normalized_model = {
        key: value
        for key, value in (
            ("provider", model_payload.get("provider", scoped.get("provider"))),
            ("model", model_payload.get("model", scoped.get("model"))),
            ("system_prompt", model_payload.get("system_prompt", scoped.get("system_prompt"))),
        )
        if value is not None
    }
    if normalized_model:
        result["model"] = normalized_model

    api_keys = scoped.get("api_keys")
    if api_keys is not None:
        result["api_keys"] = api_keys

    runtime_db_path = runtime_payload.get("db_path", scoped.get("runtime_db_path"))
    if runtime_db_path is not None:
        result["runtime_db_path"] = runtime_db_path
    stream_poll_interval = runtime_payload.get(
        "stream_poll_interval",
        scoped.get("stream_poll_interval"),
    )
    if stream_poll_interval is not None:
        result["stream_poll_interval"] = stream_poll_interval

    return result


def _payload_from_cli_overrides(cli_overrides: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    model_payload = {
        key: value
        for key, value in (
            ("provider", cli_overrides.get("provider")),
            ("model", cli_overrides.get("model")),
            ("system_prompt", cli_overrides.get("system_prompt")),
        )
        if value is not None
    }
    if model_payload:
        result["model"] = model_payload

    api_keys = _parse_cli_api_keys(
        cli_overrides.get("api_keys", ()),
        openai_api_key=cli_overrides.get("openai_api_key"),
        anthropic_api_key=cli_overrides.get("anthropic_api_key"),
        openrouter_api_key=cli_overrides.get("openrouter_api_key"),
    )
    if api_keys:
        result["api_keys"] = api_keys

    if cli_overrides.get("runtime_db_path") is not None:
        result["runtime_db_path"] = cli_overrides["runtime_db_path"]
    if cli_overrides.get("stream_poll_interval") is not None:
        result["stream_poll_interval"] = cli_overrides["stream_poll_interval"]

    return result


def _merge_config_payloads(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = {**dict(merged[key]), **dict(value)}
        else:
            merged[key] = value
    return merged
