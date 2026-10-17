"""Shared library analysis and summary execution against isolated evidence snapshots."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from reportagent.models.schemas import AnalysisResult, Assessment, Methodology
from reportagent.services import batch_analysis as batch


@pytest.fixture
def library(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'unified.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(
            Report(
                title="Portfolio study",
                source="arxiv",
                content_hash="unified",
                abstract="We evaluate portfolio optimization using out-of-sample data and transaction costs.",
                summary="Previous summary",
            )
        )
        db.commit()
    monkeypatch.setattr(batch, "init_db", lambda: None)
    monkeypatch.setattr(batch, "get_session_factory", lambda: factory)
    monkeypatch.setattr(batch.asyncio, "sleep", AsyncMock())
    yield factory
    engine.dispose()


def model(monkeypatch, during=None, fail=False):
    client = SimpleNamespace(calls=[], _client=SimpleNamespace(close=AsyncMock()))
    seen = []

    async def analyze(**kwargs):
        seen.append(kwargs)
        if fail:
            raise ValueError("model failed")
        if during:
            during()
        for phase in (
            ["metadata"]
            if kwargs["depth"] == "quick"
            else ["metadata", "methodology", "assessment"]
        ):
            if kwargs.get("progress_cb"):
                await kwargs["progress_cb"](phase, phase)
        client.calls.append({"complete": True})
        return AnalysisResult(
            research_question="Question",
            core_contribution="Contribution",
            summary="Current summary",
            methodology=Methodology(analysis_points=[{"analysis": "Method"}]),
            assessment=Assessment(strengths=["Strength"], weaknesses=["Limitation"]),
        )

    monkeypatch.setattr(batch, "LLMClient", lambda: client)
    monkeypatch.setattr(
        batch,
        "AnalysisAgent",
        lambda **_: SimpleNamespace(analyze_report_directly=analyze),
    )
    return seen


@pytest.mark.parametrize("depth", ["quick", "standard", "deep"])
def test_analysis_runs_requested_depth_and_reuses_current_saved_cache(
    library, monkeypatch, depth
):
    seen = model(monkeypatch)

    async def run():
        options = batch.AnalysisOptions(report_id=1, depth=depth)
        first = await batch.run_analysis(options, session_factory=library)
        second = await batch.run_analysis(options, session_factory=library)
        return first, second

    first, second = asyncio.run(run())
    assert len(seen) == 1
    assert seen[0]["depth"] == depth
    assert first["analyzed_ids"] == second["skipped_ids"] == [1]
    assert first["results_by_id"][1] == second["results_by_id"][1]
    assert first["results_by_id"][1]["analysis_fingerprint"]
    assert first["results_by_id"][1]["depth"] == depth
    with library() as db:
        assert json.loads(db.get(Report, 1).analysis_json) == second["results_by_id"][1]


@pytest.mark.parametrize("failure", ["model", "concurrent", "missing_material"])
def test_analysis_failure_has_no_success_result_or_saved_analysis(
    library, monkeypatch, failure
):
    def during():
        if failure == "concurrent":
            with library() as db:
                db.get(
                    Report, 1
                ).abstract = "A new material revision with opposite conclusions."
                db.commit()

    model(monkeypatch, during=during, fail=failure == "model")
    if failure == "missing_material":
        with library() as db:
            db.get(Report, 1).abstract = "未提供摘要"
            db.commit()
    result = asyncio.run(
        batch.run_analysis(
            batch.AnalysisOptions(report_id=1, depth="quick"), session_factory=library
        )
    )
    assert result["results_by_id"] == {}
    assert result["analyzed_ids"] == []
    with library() as db:
        assert db.get(Report, 1).analysis_json is None


def test_batch_cancellation_from_progress_does_not_save(library, monkeypatch):
    model(monkeypatch)
    cancelled = False

    async def progress(report_id, phase, label):
        nonlocal cancelled
        cancelled = True

    result = asyncio.run(
        batch.run_analysis(
            batch.AnalysisOptions(report_id=1, depth="quick"),
            session_factory=library,
            progress_cb=progress,
            cancel_check=lambda: cancelled,
        )
    )
    assert result["failed_ids"] == [1]
    with library() as db:
        assert db.get(Report, 1).analysis_json is None
