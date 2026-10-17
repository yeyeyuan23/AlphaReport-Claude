"""Default collection analysis must use the same evidence and save gates as batches."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from reportagent.models.schemas import (
    AnalysisResult,
    Assessment,
    ClassificationResult,
    Methodology,
    SearchResult,
    SourceType,
)
from reportagent.services import batch_analysis, collection


def complete():
    return AnalysisResult(
        research_question="Question",
        core_contribution="Contribution",
        summary="Summary",
        methodology=Methodology(
            analysis_points=[{"analysis": "The source estimates financial covariance."}]
        ),
        assessment=Assessment(
            strengths=["Describes the estimator."],
            weaknesses=["Validation scope is limited."],
        ),
    )


@pytest.fixture
def collected(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'library.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (collection, batch_analysis):
        monkeypatch.setattr(module, "init_db", lambda: None)
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    classifier = SimpleNamespace(
        classify=AsyncMock(return_value=ClassificationResult())
    )
    monkeypatch.setattr(collection, "LLMClient", lambda: object())
    monkeypatch.setattr(collection, "LLMClassifier", lambda client: classifier)
    llm = MagicMock(calls=[])
    llm._client.close = AsyncMock()
    monkeypatch.setattr(batch_analysis, "LLMClient", lambda: llm)
    agent = SimpleNamespace(analyze_report_directly=AsyncMock())
    monkeypatch.setattr(batch_analysis, "AnalysisAgent", lambda **kwargs: agent)
    # The old unsafe path is given the same controlled model output so a bypass
    # is observable as saved invalid analysis, rather than an unmocked model call.
    import reportagent.agents.analysis_agent as agents

    monkeypatch.setattr(agents, "AnalysisAgent", lambda **kwargs: agent)

    async def analyze(**kwargs):
        llm.calls.append({"complete": True})
        return complete()

    agent.analyze_report_directly.side_effect = analyze

    def run(abstracts, **kwargs):
        papers = [
            SearchResult(
                title=f"Quantitative portfolio optimization risk model {index}",
                abstract=text,
                source=SourceType.BIGQUANT,
                source_url=f"https://bigquant.com/wiki/doc/{index}",
                abstract_only=True,
            )
            for index, text in enumerate(abstracts)
        ]
        source = SimpleNamespace(
            source_type=SourceType.BIGQUANT,
            is_available=lambda: True,
            search=AsyncMock(return_value=papers),
        )
        monkeypatch.setattr(collection, "build_sources", lambda names: [source])
        result = asyncio.run(
            collection.collect_reports(
                collection.CollectionOptions(
                    topics="portfolio", sources="bigquant", days=0, **kwargs
                )
            )
        )
        with factory() as session:
            rows = session.query(Report).order_by(Report.id).all()
        return result, rows

    yield SimpleNamespace(run=run, agent=agent, llm=llm)
    engine.dispose()


def test_default_collection_skips_placeholder_without_model_and_keeps_good_sibling(
    collected,
):
    result, rows = collected.run(
        [
            "\\...",
            "A portfolio covariance estimator uses market returns and evaluates financial risk.",
        ]
    )
    assert len(rows) == 2
    assert rows[0].analysis_json is None
    assert rows[1].analysis_json
    assert json.loads(rows[1].analysis_json)["evidence_scope"] == "abstract"
    assert collected.agent.analyze_report_directly.await_count == 1
    assert result["storage"]["analyzed"] == 1
    assert result["status"] == "partial"
    assert any(
        "analysis" in error.lower() and "skip" in error.lower()
        for error in result["storage"]["errors"]
    )


def test_default_collection_rejects_incomplete_analysis_and_preserves_success(
    collected,
):
    async def analyze(**kwargs):
        collected.llm.calls.append({"complete": True})
        return (
            AnalysisResult(summary="Only summary")
            if kwargs["title"].endswith("0")
            else complete()
        )

    collected.agent.analyze_report_directly.side_effect = analyze
    result, rows = collected.run(
        ["Portfolio financial risk estimator.", "Portfolio financial risk estimator."]
    )
    assert rows[0].analysis_json is None and rows[1].analysis_json
    assert result["storage"]["analyzed"] == 1 and result["status"] == "partial"
    assert result["storage"]["errors"]


def test_default_collection_rejects_truncated_call_and_keeps_complete_sibling(
    collected,
):
    async def analyze(**kwargs):
        collected.llm.calls.append({"complete": not kwargs["title"].endswith("0")})
        return complete()

    collected.agent.analyze_report_directly.side_effect = analyze
    result, rows = collected.run(
        ["Portfolio financial risk estimator.", "Portfolio financial risk estimator."]
    )
    assert rows[0].analysis_json is None and rows[1].analysis_json
    assert result["storage"]["analyzed"] == 1 and result["status"] == "partial"


def test_collection_analysis_failure_is_safe_and_does_not_hide_success(collected):
    async def analyze(**kwargs):
        if kwargs["title"].endswith("0"):
            raise RuntimeError("Authorization: Bearer do-not-expose-this")
        collected.llm.calls.append({"complete": True})
        return complete()

    collected.agent.analyze_report_directly.side_effect = analyze
    result, rows = collected.run(
        ["Portfolio financial risk estimator.", "Portfolio financial risk estimator."]
    )
    assert rows[0].analysis_json is None and rows[1].analysis_json
    assert result["status"] == "partial" and result["storage"]["errors"]
    assert "do-not-expose-this" not in json.dumps(result)


def test_collection_reuses_valid_shared_cache_without_reporting_missing_material(
    collected,
):
    arguments = [
        "A portfolio covariance estimator uses market returns and evaluates financial risk."
    ]
    first, _ = collected.run(arguments)
    count = collected.agent.analyze_report_directly.await_count
    second, rows = collected.run(arguments)
    assert first["storage"]["analyzed"] == 1
    assert collected.agent.analyze_report_directly.await_count == count
    assert second["status"] == "ok" and second["storage"]["analyzed"] == 0
    assert rows[0].analysis_json
