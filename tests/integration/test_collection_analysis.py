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
from tests.support.collection_scoring import RelevanceModel


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
    relevance_model = RelevanceModel()
    monkeypatch.setattr(collection, "LLMClient", lambda: relevance_model)
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

    yield SimpleNamespace(
        run=run, agent=agent, llm=llm, relevance_model=relevance_model
    )
    engine.dispose()


def test_default_collection_excludes_placeholder_before_storage_and_analyzes_good_sibling(
    collected,
):
    result, rows = collected.run(
        [
            "\\...",
            "A portfolio covariance estimator uses market returns and evaluates financial risk.",
        ]
    )
    assert len(rows) == 1 and rows[0].title.endswith("1")
    assert rows[0].analysis_json
    assert json.loads(rows[0].analysis_json)["evidence_scope"] == "abstract"
    assert result["source_statuses"][0]["screening"]["pending"] == 1
    assert len(collected.relevance_model.requests) == 1
    assert (
        "标题：Quantitative portfolio optimization risk model 0"
        not in collected.relevance_model.requests[0][-1]["content"]
    )
    assert (
        "标题：Quantitative portfolio optimization risk model 1"
        in collected.relevance_model.requests[0][-1]["content"]
    )
    assert collected.agent.analyze_report_directly.await_count == 1
    assert result["storage"]["analyzed"] == 1
    assert result["status"] == "partial"
    assert any(
        "screening" in error.lower() and "not stored" in error.lower()
        for error in result["storage"]["errors"]
    )


@pytest.mark.parametrize(
    "failure", ["incomplete_analysis", "truncated_call", "exception"]
)
def test_collection_analysis_failure_preserves_complete_sibling(collected, failure):
    async def analyze(**kwargs):
        failed = kwargs["title"].endswith("0")
        if failed and failure == "exception":
            raise RuntimeError("Authorization: Bearer do-not-expose-this")
        collected.llm.calls.append(
            {"complete": not (failed and failure == "truncated_call")}
        )
        if failed and failure == "incomplete_analysis":
            return AnalysisResult(summary="Only summary")
        return complete()

    collected.agent.analyze_report_directly.side_effect = analyze
    result, rows = collected.run(
        ["Portfolio financial risk estimator.", "Portfolio financial risk estimator."]
    )
    assert rows[0].analysis_json is None and rows[1].analysis_json
    assert result["storage"]["analyzed"] == 1 and result["status"] == "partial"
    assert result["storage"]["errors"]
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
