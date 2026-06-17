from __future__ import annotations

from fastapi import FastAPI

from .app import create_app, create_local_platform_api_from_config
from .config import load_api_config_from_env, load_runtime_config_from_env


def build_app() -> FastAPI:
    api_config = load_api_config_from_env()
    runtime_config = load_runtime_config_from_env()
    platform_api = create_local_platform_api_from_config(runtime_config)
    return create_app(platform_api=platform_api, config=api_config)


app = build_app()


__all__ = ["app", "build_app"]
