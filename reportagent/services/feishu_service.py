"""Shared configuration and REST client for authorized Feishu delivery."""

from __future__ import annotations

import os

from reportagent.utils.config import get_config


def _get_feishu_config() -> dict:
    return get_config("feishu", default={})


def _get_credentials() -> tuple[str, str]:
    cfg = _get_feishu_config()
    app_id = os.environ.get(cfg.get("app_id_env", "FEISHU_APP_ID"), "")
    app_secret = os.environ.get(cfg.get("app_secret_env", "FEISHU_APP_SECRET"), "")
    return app_id, app_secret


def is_configured() -> bool:
    app_id, app_secret = _get_credentials()
    return bool(app_id and app_secret)


def _build_client():
    import lark_oapi as lark

    app_id, app_secret = _get_credentials()
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
