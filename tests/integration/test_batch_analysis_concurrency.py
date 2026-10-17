"""Analysis must belong to the exact material observed before a model request."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.api import library
from reportagent.models.database import Base, Report
from reportagent.models.schemas import AnalysisResult
from reportagent.services import batch_analysis as batch


@pytest.fixture
def analysis_library(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'library.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(
            Report(
                title="Original portfolio research",
                abstract="We evaluate portfolio allocation with trading costs and out-of-sample data.",
                full_text="Original complete research material. " * 100,
                has_full_text=True,
                arxiv_id="2601.12345v1",
                source="arxiv",
                content_hash="paper",
            )
        )
        db.commit()
    monkeypatch.setattr(batch, "init_db", lambda: None)
    monkeypatch.setattr(batch, "get_session_factory", lambda: factory)
    monkeypatch.setattr(batch.asyncio, "sleep", AsyncMock())
    yield factory
    engine.dispose()


def install_model(monkeypatch, during_analysis):
    client = SimpleNamespace(calls=[], _client=SimpleNamespace(close=AsyncMock()))

    async def analyze(**kwargs):
        during_analysis()
        client.calls.append({"complete": True})
        return AnalysisResult(
            research_question="Old question",
            core_contribution="Old contribution",
            summary="OLD conclusion",
        )

    monkeypatch.setattr(batch, "LLMClient", lambda: client)
    monkeypatch.setattr(
        batch,
        "AnalysisAgent",
        lambda **_: SimpleNamespace(
            analyze_report_directly=AsyncMock(side_effect=analyze)
        ),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "New title"),
        ("abstract", "New abstract with different conclusions"),
        ("full_text", "Revised full text with the opposite finding. " * 100),
        ("has_full_text", False),
        ("arxiv_id", "2601.12345v2"),
        ("equations_json", '[{"latex": "new equation"}]'),
        ("analysis_json", '{"summary": "Newer successful analysis"}'),
    ],
)
def test_analysis_does_not_overwrite_a_concurrent_update(
    analysis_library, monkeypatch, field, value
):
    def update():
        with analysis_library() as db:
            row = db.get(Report, 1)
            setattr(row, field, value)
            row.knowledge_card_json = '{"summary": "newer card"}'
            db.commit()

    install_model(monkeypatch, update)
    audit = asyncio.run(
        batch.run_analysis(batch.AnalysisOptions(report_id=1, depth="quick"))
    )
    assert audit["failed_ids"] == [1]
    assert audit["analyzed_ids"] == []
    assert audit["errors"][1]
    with analysis_library() as db:
        row = db.get(Report, 1)
        assert getattr(row, field) == value
        assert row.analysis_json == (value if field == "analysis_json" else None)
        assert row.knowledge_card_json == '{"summary": "newer card"}'


def test_analysis_reports_a_deleted_row_as_failed(analysis_library, monkeypatch):
    def delete():
        with analysis_library() as db:
            db.delete(db.get(Report, 1))
            db.commit()

    install_model(monkeypatch, delete)
    audit = asyncio.run(
        batch.run_analysis(batch.AnalysisOptions(report_id=1, depth="quick"))
    )
    assert audit["failed_ids"] == [1]
    assert audit["analyzed"] == 0
    with analysis_library() as db:
        assert db.get(Report, 1) is None


@pytest.mark.parametrize("reader", ["current", "briefing"])
def test_readers_hide_stale_batch_analysis_without_destroying_history(
    analysis_library, reader
):
    with analysis_library() as db:
        row = db.get(Report, 1)
        analysis = {
            "research_question": "Old question",
            "core_contribution": "Old contribution",
            "summary": "Old conclusion",
            "depth": "quick",
            "analysis_fingerprint": batch.analysis_cache_fingerprint(row, "quick"),
        }
        row.analysis_json = json.dumps(analysis)
        row.title = "New title"
        db.commit()
        saved = row.analysis_json
        if reader == "current":
            assert "analysis" not in library.get_report(1, db)["data"]
        else:
            result = asyncio.run(library.get_research_briefing(1, False, db))["data"]
            assert result["source"] == "empty"
            assert result["sections"] == []
        assert row.analysis_json == saved


@pytest.mark.parametrize("signed", [False, True])
def test_current_and_legacy_analysis_remain_readable(analysis_library, signed):
    with analysis_library() as db:
        row = db.get(Report, 1)
        analysis = {
            "research_question": "Question",
            "summary": "Current conclusion",
            "depth": "quick",
        }
        if signed:
            analysis["analysis_fingerprint"] = batch.analysis_cache_fingerprint(
                row, "quick"
            )
        row.analysis_json = json.dumps(analysis)
        db.commit()
        assert library.get_report(1, db)["data"]["analysis"] == analysis


def test_analysis_does_not_save_under_changed_model_policy(
    analysis_library, monkeypatch
):
    install_model(
        monkeypatch,
        lambda: monkeypatch.setattr(batch, "get_llm_model", lambda: "changed-model"),
    )
    audit = asyncio.run(
        batch.run_analysis(batch.AnalysisOptions(report_id=1, depth="quick"))
    )
    assert audit["failed_ids"] == [1]
    with analysis_library() as db:
        assert db.get(Report, 1).analysis_json is None


def test_changed_policy_cannot_label_reused_client_results_as_current(
    analysis_library, monkeypatch
):
    with analysis_library() as db:
        db.add(
            Report(
                title="Second portfolio study",
                abstract="We evaluate portfolio allocation with trading costs and out-of-sample data.",
                source="arxiv",
                content_hash="second",
            )
        )
        db.commit()
    model_calls = []

    def change_policy():
        model_calls.append("old client")
        monkeypatch.setattr(batch, "get_llm_model", lambda: "changed-model")

    install_model(monkeypatch, change_policy)
    audit = asyncio.run(
        batch.run_analysis(batch.AnalysisOptions(all=True, depth="quick"))
    )
    assert set(audit["failed_ids"]) == {1, 2}
    assert audit["analyzed_ids"] == []
    assert model_calls == ["old client"]
    with analysis_library() as db:
        assert all(row.analysis_json is None for row in db.query(Report))
