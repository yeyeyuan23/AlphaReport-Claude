"""Invalid model fields must be actionable without logging transport secrets."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reportagent.classifiers.llm_classifier import LLMClassifier
from reportagent.classifiers.provenance import input_fingerprint


def test_invalid_enum_logs_field_value_allowed_values_and_input(caplog):
    client = SimpleNamespace(chat_json=AsyncMock(return_value={"markets": ["US"]}))
    with caplog.at_level(logging.WARNING), pytest.raises(ValueError) as caught:
        asyncio.run(LLMClassifier(client).classify("Paper title", "Abstract"))
    detail = str(caught.value)
    assert all(value in detail for value in ("markets", "US", "overseas", "global"))
    assert "Paper title" in caplog.text
    assert input_fingerprint("Paper title", "Abstract") in caplog.text
    assert detail in caplog.text


def test_unknown_provider_exception_does_not_log_credentials(caplog):
    client = SimpleNamespace(
        chat_json=AsyncMock(side_effect=RuntimeError("api_key=secret-value"))
    )
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
        asyncio.run(LLMClassifier(client).classify("Paper title", "Abstract"))
    assert "RuntimeError" in caplog.text
    assert "secret-value" not in caplog.text


def frequency_response(value):
    return {
        "markets": [],
        "asset_classes": [],
        "frequencies": [value],
        "topics": [],
        "custom_tags": [],
        "confidence": 0.8,
        "evidence": {
            f"frequencies.{value}": {
                "source": "material",
                "quote": "high-frequency return intervals",
                "reason": "Explicit high-frequency returns",
            }
        },
    }


def test_observed_high_freq_response_is_corrected_by_model_and_reverified():
    client = SimpleNamespace(
        chat_json=AsyncMock(
            side_effect=[frequency_response("high_freq"), frequency_response("hft")]
        )
    )
    result = asyncio.run(
        LLMClassifier(client).classify(
            "Post-call", "We study high-frequency return intervals."
        )
    )
    assert [value.value for value in result.frequencies] == ["hft"]
    assert result.provenance["schema_repair_status"] == "completed"
    feedback = client.chat_json.call_args_list[1].args[0][-1]["content"]
    assert "high_freq" in feedback and "hft" in feedback
    assert client.chat_json.await_count == 2
    assert (
        result.provenance["labels"]["frequencies.hft"]["quote"]
        == "high-frequency return intervals"
    )


def test_schema_repair_is_bounded_and_does_not_silently_alias_invalid_values():
    client = SimpleNamespace(
        chat_json=AsyncMock(return_value=frequency_response("high_freq"))
    )
    with pytest.raises(ValueError, match="high_freq"):
        asyncio.run(
            LLMClassifier(client).classify(
                "Post-call", "high-frequency return intervals"
            )
        )
    assert client.chat_json.await_count == 2


def test_schema_repair_cannot_bypass_original_text_evidence_check():
    client = SimpleNamespace(
        chat_json=AsyncMock(
            side_effect=[frequency_response("high_freq"), frequency_response("hft")]
        )
    )
    with pytest.raises(ValueError, match="evidence"):
        asyncio.run(
            LLMClassifier(client).classify("Post-call", "No frequency supplied.")
        )
    assert client.chat_json.await_count == 2


def test_collection_receipt_identifies_paper_and_invalid_field():
    from reportagent.classifiers.rule_classifier import RuleClassifier
    from reportagent.models.schemas import SearchResult, SourceType
    from reportagent.services.collection import classify_for_storage

    client = SimpleNamespace(
        chat_json=AsyncMock(return_value=frequency_response("high_freq"))
    )
    errors = []
    paper = SearchResult(
        title="Post-call",
        abstract="high-frequency return intervals",
        source=SourceType.ARXIV,
        arxiv_id="2610.21659v1",
    )
    result = asyncio.run(
        classify_for_storage(
            paper,
            RuleClassifier(),
            LLMClassifier(client),
            errors,
            check_domain=False,
        )
    )
    assert len(errors) == 1
    assert all(
        value in errors[0]
        for value in ("2610.21659v1", "frequencies", "high_freq", "hft")
    )
    assert result.classification.provenance["last_failed_attempt"]["status"] == "failed"
