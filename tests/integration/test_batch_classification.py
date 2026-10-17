"""Whole-library classification must not inherit the API's page-size ceiling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from reportagent.models.schemas import ClassificationResult, Topic
from reportagent.services import batch_classification as classification


def test_classify_all_visits_more_than_one_page_and_closes_client(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        with factory() as session:
            session.add_all(
                [
                    Report(title=f"paper-{i}", content_hash=str(i), source="arxiv")
                    for i in range(207)
                ]
            )
            session.commit()
        monkeypatch.setattr(classification, "init_db", lambda: None)
        monkeypatch.setattr(classification, "get_session_factory", lambda: factory)
        client = MagicMock()
        client._client.close = AsyncMock()
        monkeypatch.setattr(classification, "LLMClient", lambda: client)
        model = MagicMock()
        model.classify = AsyncMock(
            return_value=ClassificationResult(
                topics=[Topic.FACTOR_MODEL], method="llm", confidence=0.8
            )
        )
        monkeypatch.setattr(classification, "LLMClassifier", lambda _: model)
        monkeypatch.setattr(classification.asyncio, "sleep", AsyncMock())
        asyncio.run(
            classification.classify_async(
                classification.ClassificationOptions(all=True, llm=True)
            )
        )
        assert model.classify.await_count == 207
        with factory() as session:
            assert (
                session.query(Report).filter(Report.topics == "factor_model").count()
                == 207
            )
        client._client.close.assert_awaited_once()
    finally:
        engine.dispose()


def test_hybrid_does_not_depend_on_discarded_rule_guesses(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        with factory() as db:
            db.add(Report(title="Risk research", content_hash="hybrid", source="arxiv"))
            db.commit()
        monkeypatch.setattr(classification, "init_db", lambda: None)
        monkeypatch.setattr(classification, "get_session_factory", lambda: factory)
        client = MagicMock()
        client._client.close = AsyncMock()
        monkeypatch.setattr(classification, "LLMClient", lambda: client)
        model = MagicMock()
        model.classify = AsyncMock(
            return_value=ClassificationResult(
                topics=[Topic.FACTOR_MODEL], method="llm", confidence=0.8
            )
        )
        monkeypatch.setattr(classification, "LLMClassifier", lambda _: model)

        class UnusableRules:
            def classify(self, *args):
                raise RuntimeError("rules are irrelevant to the model's decision")

        monkeypatch.setattr(classification, "RuleClassifier", UnusableRules)
        audit = asyncio.run(
            classification.classify_async(
                classification.ClassificationOptions(all=True, hybrid=True)
            )
        )
        assert audit["classified"] == 1 and audit["failed"] == 0
        with factory() as db:
            row = db.get(Report, 1)
            assert row.topics == "factor_model"
            assert row.classification_method == "hybrid"
            assert row.classification_confidence == 0.8
    finally:
        engine.dispose()
