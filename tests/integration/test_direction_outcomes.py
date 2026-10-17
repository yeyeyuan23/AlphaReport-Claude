"""Both scoring commands preserve valid siblings and expose failed work."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.filters.direction_filter import DirectionFilter
from reportagent.models.database import Base, Report
from reportagent.services import direction_scoring, paper_scoring

ABSTRACT = (
    "We study conditional portfolio risk forecasts and evaluate them out of sample."
)


@pytest.fixture
def library(tmp_path, monkeypatch):
    config = {"directions": [{"id": "risk", "name": "Risk"}]}
    monkeypatch.setattr(paper_scoring, "_load_scoring_config", lambda: config)
    engine = create_engine(f"sqlite:///{tmp_path / 'scores.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (direction_scoring, paper_scoring):
        monkeypatch.setattr(module, "init_db", lambda: None)
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    with factory() as db:
        db.add_all(
            [
                Report(
                    title=f"Risk {i}",
                    abstract=ABSTRACT,
                    source="arxiv",
                    content_hash=str(i),
                )
                for i in (1, 2)
            ]
        )
        db.commit()
    yield factory
    engine.dispose()


def assessment(index):
    return {
        "index": index,
        "direction_scores": [
            {"direction_id": "risk", "score": 8, "reason": "Risk forecasts."}
        ],
    }


def model_stub(monkeypatch, response):
    from reportagent.llm import client

    model = SimpleNamespace(
        provider="offline",
        model="fixture",
        _client=SimpleNamespace(close=AsyncMock()),
        chat_json=AsyncMock(side_effect=response),
    )
    monkeypatch.setattr(client, "LLMClient", lambda: model)
    monkeypatch.setattr(paper_scoring, "LLMClient", lambda: model)
    return model


def run(entrypoint, *, dry_run=False):
    if entrypoint == "direction":
        return asyncio.run(
            direction_scoring.run_direction_filter(
                direction_scoring.DirectionOptions(json_output=True, dry_run=dry_run)
            )
        )
    return asyncio.run(
        paper_scoring.score_reports(
            paper_scoring.ScoringOptions(all_unjudged=True, dry_run=dry_run)
        )
    )


@pytest.mark.parametrize("entrypoint", ["direction", "canonical"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_partial_model_failure_raises_after_preserving_valid_sibling(
    library, monkeypatch, entrypoint, dry_run
):
    async def response(*args, **kwargs):
        return [assessment(0)]  # The second eligible paper is missing.

    model_stub(monkeypatch, response)
    with pytest.raises(RuntimeError, match="scoring failed for 1 paper"):
        run(entrypoint, dry_run=dry_run)
    with library() as db:
        rows = db.query(Report).all()
        assert sum(row.selection_relevance == 8 for row in rows) == (
            0 if dry_run else 1
        )
        assert sum(row.selection_relevance is None for row in rows) == (
            2 if dry_run else 1
        )


@pytest.mark.parametrize("entrypoint", ["direction", "canonical"])
def test_persistence_failure_raises_without_losing_valid_sibling(
    library, monkeypatch, entrypoint
):
    async def response(*args, **kwargs):
        with library() as db:
            db.get(Report, 1).abstract = ABSTRACT + " Corrected material."
            db.commit()
        return [assessment(0), assessment(1)]

    model_stub(monkeypatch, response)
    with pytest.raises(RuntimeError, match="Failed to persist 1 relevance"):
        run(entrypoint)
    with library() as db:
        assert db.get(Report, 1).selection_relevance is None
        assert db.get(Report, 2).selection_relevance == 8


@pytest.mark.parametrize("entrypoint", ["direction", "canonical"])
def test_total_model_failure_is_not_success(library, monkeypatch, entrypoint):
    async def response(*args, **kwargs):
        raise RuntimeError("model unavailable")

    model_stub(monkeypatch, response)
    with pytest.raises(RuntimeError, match="model unavailable"):
        run(entrypoint)
    with library() as db:
        assert all(row.selection_relevance is None for row in db.query(Report))


@pytest.mark.parametrize("entrypoint", ["direction", "canonical"])
def test_pending_material_is_a_successful_recorded_outcome(
    library, monkeypatch, entrypoint
):
    with library() as db:
        for row in db.query(Report):
            row.abstract = ""
        db.commit()

    def forbidden():
        pytest.fail("Missing material must not initialize a model")

    from reportagent.llm import client

    monkeypatch.setattr(client, "LLMClient", forbidden)
    monkeypatch.setattr(paper_scoring, "LLMClient", forbidden)
    run(entrypoint)
    with library() as db:
        assert all(
            json.loads(row.selection_provenance_json)["status"]
            == "pending_needs_material"
            for row in db.query(Report)
        )


def test_direction_adapter_keeps_canonical_failure_audit(library):
    model = SimpleNamespace(chat_json=AsyncMock(return_value=[assessment(0)]))
    filt = DirectionFilter(model)
    papers = [{"id": i, "title": f"Risk {i}", "abstract": ABSTRACT} for i in (1, 2)]
    results = asyncio.run(filt.score_papers(papers))
    assert [result.status for result in results] == ["scored", "retryable_error"]
    assert len(filt.failed_batches) == 1
    assert filt.failed_batches[0][:2] == (0, 1)
    assert "missing" in filt.failed_batches[0][2]
