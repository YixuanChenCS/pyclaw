from __future__ import annotations

import os
from pathlib import Path
import json
from typing import Mapping

from services.agent_core import LocalAgentRunnerConfig, resolve_local_agent_runner_config

from .schemas import APIAuthPrincipal, APIConfig


def load_api_config_from_env(
    env: Mapping[str, str] | None = None,
    *,
    cwd: str | Path | None = None,
) -> APIConfig:
    source = env if env is not None else os.environ
    token = _normalize_optional_value(source.get("PYCLAW_API_BEARER_TOKEN"))
    auth_principals = _parse_auth_principals(source.get("PYCLAW_API_AUTH_PRINCIPALS"))

    raw_origins = source.get("PYCLAW_API_ALLOWED_ORIGINS")
    if raw_origins is None:
        origins = APIConfig().cors_allowed_origins
    else:
        origins = _parse_csv(raw_origins)

    raw_roots = source.get("PYCLAW_API_WORKSPACE_ROOTS")
    if raw_roots is None:
        default_root = Path(cwd or Path.cwd()).resolve(strict=False)
        roots = (_validate_workspace_root(default_root, source_name="current working directory"),)
    else:
        entries = _parse_csv(raw_roots)
        if not entries:
            raise ValueError("PYCLAW_API_WORKSPACE_ROOTS must contain at least one directory.")
        roots = tuple(
            _validate_workspace_root(Path(entry).expanduser(), source_name="PYCLAW_API_WORKSPACE_ROOTS")
            for entry in entries
        )

    return APIConfig(
        allowed_workspace_roots=roots,
        api_token=token,
        auth_principals=auth_principals,
        cors_allowed_origins=origins,
    )


def load_runtime_config_from_env(
    env: Mapping[str, str] | None = None,
    *,
    workspace_root: str | Path | None = ".",
) -> LocalAgentRunnerConfig:
    source = dict(env if env is not None else os.environ)
    api_db_path = _normalize_optional_value(source.get("PYCLAW_API_DB_PATH"))
    if api_db_path is not None:
        source["EXECUTION_RUNTIME_DB_PATH"] = api_db_path
    return resolve_local_agent_runner_config(workspace_root=workspace_root, env=source)


def _parse_csv(raw_value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw_value.split(",") if part.strip())


def _normalize_optional_value(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    value = raw_value.strip()
    return value or None


def _validate_workspace_root(path: Path, *, source_name: str) -> str:
    resolved = path.resolve(strict=False)
    if not resolved.exists():
        raise ValueError(f"{source_name} contains a workspace root that does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"{source_name} contains a workspace root that is not a directory: {resolved}")
    return str(resolved)


def _parse_auth_principals(raw_value: str | None) -> tuple[APIAuthPrincipal, ...]:
    value = _normalize_optional_value(raw_value)
    if value is None:
        return ()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("PYCLAW_API_AUTH_PRINCIPALS must be valid JSON.") from exc
    if not isinstance(payload, list):
        raise ValueError("PYCLAW_API_AUTH_PRINCIPALS must be a JSON array.")
    principals: list[APIAuthPrincipal] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise ValueError("PYCLAW_API_AUTH_PRINCIPALS entries must be JSON objects.")
        principals.append(APIAuthPrincipal.model_validate(item))
    return tuple(principals)


__all__ = [
    "load_api_config_from_env",
    "load_runtime_config_from_env",
]
