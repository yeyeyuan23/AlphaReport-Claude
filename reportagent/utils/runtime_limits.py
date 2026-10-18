"""Validated execution budgets, independent of research and cache identities."""

import math

from reportagent.utils.config import get_config


def positive_number(value, name: str, *, integer: bool = False):
    valid_type = type(value) is int if integer else type(value) in (int, float)
    if not valid_type or not math.isfinite(value) or value <= 0:
        kind = "integer" if integer else "number"
        raise ValueError(f"{name} must be a finite positive {kind}")
    return value


def positive_setting(*keys: str, default, integer: bool = False):
    return positive_number(
        get_config(*keys, default=default), ".".join(keys), integer=integer
    )


def metadata_timeout_seconds():
    return positive_setting(
        "collection", "metadata", "request_timeout_seconds", default=30
    )


def metadata_max_bytes() -> int:
    size = positive_setting("collection", "metadata", "max_size_mb", default=5)
    maximum = int(size * 1024 * 1024)
    if maximum < 1:
        raise ValueError("collection.metadata.max_size_mb must allow at least one byte")
    return maximum


def review_timeout_seconds():
    return positive_setting("llm", "full_text_review", "timeout_seconds", default=240)
