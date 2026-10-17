"""Malformed/truncated model classifications must not erase existing tags."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.classifiers.llm_classifier import LLMClassifier
from reportagent.llm.client import LLMClient
from reportagent.models.database import Base, Report
from reportagent.services import batch_classification as classification

VALID = {
    "markets": [],
    "asset_classes": [],
    "frequencies": [],
    "topics": ["factor_model"],
    "custom_tags": [],
    "confidence": 0.8,
    "evidence": {
        "topics.factor_model": {
            "source": "material",
            "quote": "factor model",
            "reason": "Explicit factor model research",
        }
    },
}


@pytest.mark.parametrize(
    "result",
    [
        {},
        [],
        {"topics": "factor_model"},
        {**VALID, "topics": ["invented"]},
        {**VALID, "confidence": float("nan")},
        {**VALID, "topics": [], "confidence": 0},
        {**VALID, "topics": []},
    ],
)
def test_invalid_classification_raises_instead_of_empty_llm_success(result):
    client = SimpleNamespace(chat_json=AsyncMock(return_value=result))
    with pytest.raises(ValueError):
        asyncio.run(LLMClassifier(client).classify("Portfolio model", ""))


def test_model_error_reaches_caller():
    client = SimpleNamespace(
        chat_json=AsyncMock(side_effect=RuntimeError("max_tokens"))
    )
    with pytest.raises(RuntimeError, match="max_tokens"):
        asyncio.run(LLMClassifier(client).classify("Portfolio model", ""))


def test_truncated_openai_json_cannot_be_treated_as_complete():
    client = object.__new__(LLMClient)
    client.provider, client.model, client.calls = "deepseek", "test", []
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content='{"topics": []}'),
            )
        ],
        model="test",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
    )
    client._client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=response))
        )
    )
    with pytest.raises(ValueError, match="Incomplete"):
        asyncio.run(client.chat_json([{"role": "user", "content": "Classify"}]))
    assert client.calls[-1]["complete"] is False


def test_one_model_failure_preserves_existing_tags_and_successful_siblings(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add_all(
            [
                Report(
                    title=title,
                    abstract="We study a factor model.",
                    source="arxiv",
                    content_hash=title,
                    topics="risk_model",
                )
                for title in ["fail", "succeed"]
            ]
        )
        session.commit()
    monkeypatch.setattr(classification, "init_db", lambda: None)
    monkeypatch.setattr(classification, "get_session_factory", lambda: factory)

    async def respond(messages, **kwargs):
        if "## 研报标题\nfail" in messages[1]["content"]:
            raise RuntimeError("max_tokens")
        return VALID

    client = SimpleNamespace(
        chat_json=respond, _client=SimpleNamespace(close=AsyncMock())
    )
    monkeypatch.setattr(classification, "LLMClient", lambda: client)
    monkeypatch.setattr(classification.asyncio, "sleep", AsyncMock())
    audit = asyncio.run(
        classification.classify_async(
            classification.ClassificationOptions(all=True, llm=True)
        )
    )
    assert audit["failed_ids"] == [1] and audit["classified_ids"] == [2]
    assert "max_tokens" in audit["errors"][1]
    with factory() as session:
        assert session.get(Report, 1).topics == "risk_model"
        assert session.get(Report, 2).topics == "factor_model"
    engine.dispose()


@pytest.mark.parametrize("method", ["chat", "chat_json"])
@pytest.mark.parametrize("stop, content", [("length", '{"topics": []}'), ("stop", "")])
def test_openai_incomplete_text_or_json_cannot_succeed(method, stop, content):
    client = object.__new__(LLMClient)
    client.provider, client.model, client.calls = "deepseek", "test", []
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=stop, message=SimpleNamespace(content=content)
            )
        ],
        model="test",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
    )
    client._client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=response))
        )
    )
    with pytest.raises(ValueError, match="Incomplete"):
        asyncio.run(getattr(client, method)([{"role": "user", "content": "Read"}]))
    assert client.calls[-1]["complete"] is False


def test_complete_openai_text_records_complete_call():
    client = object.__new__(LLMClient)
    client.provider, client.model, client.calls = "deepseek", "test", []
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="Evidence summary"),
            )
        ],
        model="test",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
    )
    client._client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=response))
        )
    )
    assert (
        asyncio.run(client.chat([{"role": "user", "content": "Read"}]))
        == "Evidence summary"
    )
    assert client.calls[-1]["complete"] is True
