from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache
from typing import Optional, Any

import yaml
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _deep_merge(base: dict, override: dict) -> dict:
    merged = base.copy()
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


@lru_cache(maxsize=1)
def load_config(config_path  : Optional[str] = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else PROJECT_ROOT / "configs" / "app.yaml"
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return cfg


def get_config(*keys: str, default: Any = None) -> Any:
    cfg = load_config()
    for k in keys:
        if isinstance(cfg, dict):
            cfg = cfg.get(k)
        else:
            return default
        if cfg is None:
            return default
    return cfg


def get_db_path() -> Path:
    rel = get_config("database", "path", default="data/report_library.db")
    return PROJECT_ROOT / rel


def get_llm_api_key() -> str:
    env_name = get_config("llm", "api_key_env", default="OPENAI_API_KEY")
    return os.environ.get(env_name, "")


def get_llm_base_url() -> Optional[str]:
    env_name = get_config("llm", "base_url_env", default="OPENAI_BASE_URL")
    return os.environ.get(env_name)


def get_llm_model() -> str:
    return get_config("llm", "model", default="gpt-4o-mini")


def get_llm_provider() -> str:
    return get_config("llm", "provider", default="openai")
