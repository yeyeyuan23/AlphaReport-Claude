"""Corrected classifications must replace stale columns and tag associations together."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Report, Tag
from reportagent.models.schemas import (
    ClassifiedReport,
    ClassificationResult,
    SearchResult,
    SourceType,
    Topic,
)
from reportagent.services import batch_classification as batch


@pytest.fixture
def library(expiring_memory_session_factory):
    factory = expiring_memory_session_factory
    with factory() as session:
        report = Report(
            title="Stock prediction",
            source="local_pdf",
            content_hash="fixture",
            markets="china",
            topics="ai_ml_model,statistical_arbitrage",
        )
        report.tags = [
            Tag(dimension="market", value="china"),
            Tag(dimension="topic", value="statistical_arbitrage"),
            Tag(dimension="custom", value="pairs trading"),
        ]
        session.add(report)
        session.commit()
    return factory


def corrected():
    return ClassificationResult(
        topics=[Topic.AI_ML_MODEL],
        custom_tags=["graph learning"],
        confidence=0.8,
        method="llm",
        provenance={
            "labels": {
                "topics.ai_ml_model": {
                    "source": "material",
                    "quote": "graph learning",
                    "reason": "Learned model",
                }
            }
        },
    )


def assert_corrected(report):
    assert report.markets == ""
    assert report.topics == "ai_ml_model"
    assert {(tag.dimension, tag.value) for tag in report.tags} == {
        ("topic", "ai_ml_model"),
        ("custom", "graph learning"),
    }
    assert json.loads(report.classification_provenance_json) == corrected().provenance


def test_reimport_clears_unsupported_old_labels_and_persists_evidence(library):
    with library() as session:
        report = session.get(Report, 1)
        sr = SearchResult(title=report.title, source=SourceType.LOCAL_PDF)
        from reportagent.utils.hashing import content_hash

        report.content_hash = content_hash(sr.title, [])
        session.commit()
        ReportRepository(session).upsert_report(
            ClassifiedReport(search_result=sr, classification=corrected())
        )
    with library() as session:
        assert_corrected(session.get(Report, 1))


def test_batch_reclassification_replaces_relations_and_refreshes_evidence(
    library, monkeypatch
):
    monkeypatch.setattr(batch, "init_db", lambda: None)
    monkeypatch.setattr(batch, "get_session_factory", lambda: library)
    client = SimpleNamespace(_client=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(batch, "LLMClient", lambda: client)
    monkeypatch.setattr(
        batch,
        "LLMClassifier",
        lambda _: SimpleNamespace(classify=AsyncMock(return_value=corrected())),
    )
    audit = asyncio.run(
        batch.classify_async(batch.ClassificationOptions(report_id=1, llm=True))
    )
    assert audit["failed"] == 0 and audit["updated"] == 1
    with library() as session:
        assert_corrected(session.get(Report, 1))
        session.get(Report, 1).classification_provenance_json = None
        session.commit()
    # Evidence alone can change even when the selected values stay the same.
    audit = asyncio.run(
        batch.classify_async(batch.ClassificationOptions(report_id=1, llm=True))
    )
    assert audit["updated"] == 1
    with library() as session:
        assert_corrected(session.get(Report, 1))


def test_batch_dry_run_keeps_columns_tags_and_evidence_unchanged(library, monkeypatch):
    monkeypatch.setattr(batch, "init_db", lambda: None)
    monkeypatch.setattr(batch, "get_session_factory", lambda: library)
    monkeypatch.setattr(
        batch,
        "LLMClient",
        lambda: SimpleNamespace(_client=SimpleNamespace(close=AsyncMock())),
    )
    monkeypatch.setattr(
        batch,
        "LLMClassifier",
        lambda _: SimpleNamespace(classify=AsyncMock(return_value=corrected())),
    )
    audit = asyncio.run(
        batch.classify_async(
            batch.ClassificationOptions(report_id=1, llm=True, dry_run=True)
        )
    )
    assert audit["updated"] == 1
    with library() as session:
        report = session.get(Report, 1)
        assert (
            report.markets == "china"
            and report.topics == "ai_ml_model,statistical_arbitrage"
        )
        assert report.classification_provenance_json is None
        assert {(tag.dimension, tag.value) for tag in report.tags} == {
            ("market", "china"),
            ("topic", "statistical_arbitrage"),
            ("custom", "pairs trading"),
        }
