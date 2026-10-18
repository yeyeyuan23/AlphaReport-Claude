"""Persist model output and parser evidence without request headers or credentials."""

from datetime import datetime, timezone
import json
from pathlib import Path
import os
from uuid import uuid4

from reportagent.utils.config import PROJECT_ROOT

RESPONSE_ROOT = PROJECT_ROOT / "data/model_responses"


def save_response(
    text: str, response, *, provider: str, call: dict, context: dict | None = None
) -> Path:
    now = datetime.now(timezone.utc)
    directory = RESPONSE_ROOT / now.strftime("%Y%m%d") / str(uuid4())
    directory.mkdir(parents=True)
    (directory / "response.txt").write_text(text, encoding="utf-8", newline="")
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        (directory / "response.json").write_text(
            json.dumps(dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    metadata = {
        "recorded_at": now.isoformat(),
        "provider": provider,
        "process_id": os.getpid(),
    }
    metadata.update(
        {
            key: context[key]
            for key in ("task", "report_id")
            if context and key in context
        }
    )
    metadata.update(
        {
            key: call[key]
            for key in (
                "model",
                "stop_reason",
                "input_tokens",
                "output_tokens",
                "complete",
            )
            if key in call
        }
    )
    (directory / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return directory


def save_parse_error(directory: Path, error: json.JSONDecodeError):
    (directory / "parse-input.txt").write_text(error.doc, encoding="utf-8", newline="")
    details = {
        "exception": type(error).__name__,
        "message": error.msg,
        "line": error.lineno,
        "column": error.colno,
        "position": error.pos,
        "context_before": error.doc[max(0, error.pos - 160) : error.pos],
        "context_after": error.doc[error.pos : error.pos + 160],
        "parse_input_file": "parse-input.txt",
        "raw_response_file": "response.txt",
    }
    (directory / "parse-error.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_validation_error(directory: Path, error, data):
    (directory / "validation-input.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    details = {
        "exception": type(error).__name__,
        "schema": error.title,
        "errors": json.loads(error.json(include_url=False)),
        "validation_input_file": "validation-input.json",
        "raw_response_file": "response.txt",
    }
    (directory / "validation-error.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )
