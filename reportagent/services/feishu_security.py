"""Shared authorization for Feishu's WebSocket and optional HTTP transport."""
from __future__ import annotations

import hmac
import os


def http_callback_enabled() -> bool:
    return os.environ.get("FEISHU_HTTP_CALLBACK_ENABLED", "false").lower() == "true"


def sender_allowed(open_id: str) -> bool:
    allowed = {item.strip() for item in os.environ.get("FEISHU_ALLOWED_OPEN_IDS", "").split(",") if item.strip()}
    return bool(open_id and open_id in allowed)


def secret_matches(expected: str, supplied: str) -> bool:
    return bool(expected and supplied and hmac.compare_digest(expected.encode(), supplied.encode()))
