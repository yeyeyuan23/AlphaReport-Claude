"""Direction detail and ordinary relevance share one assessment and persistence."""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from reportagent.services import paper_scoring as scoring, direction_scoring
from reportagent.filters.direction_filter import DirectionFilter

ABSTRACT = "We study portfolio risk forecasting and evaluate allocation out of sample."


@pytest.fixture
def rubric(monkeypatch):
    config = {
        "directions": [
            {"id": "risk", "name": "Risk"},
            {"id": "factor", "name": "Factor"},
        ],
        "push_settings": {"llm_filter_threshold": 6},
    }
    monkeypatch.setattr(scoring, "_load_scoring_config", lambda: config)
    return config


class Model:
    async def chat_json(self, messages, **kwargs):
        return [
            {
                "index": 0,
                "status": "scored",
                "direction_scores": [
                    {"direction_id": "risk", "score": 8, "reason": "Risk forecasts."},
                    {
                        "direction_id": "factor",
                        "score": 2,
                        "reason": "No factor construction.",
                    },
                ],
            }
        ]


def test_direction_detail_is_the_same_canonical_assessment(rubric):
    paper = {"id": 1, "title": "Portfolio research", "abstract": ABSTRACT}
    ordinary, failures = asyncio.run(scoring.score_papers_batch(Model(), [paper]))
    detailed = asyncio.run(DirectionFilter(Model()).score_papers([paper]))[0]
    assert not failures
    assert ordinary[0]["relevance"] == detailed.max_score == 8
    assert ordinary[0]["best_direction"] == detailed.best_direction == "risk"
    assert len(detailed.direction_scores) == 2
    assert detailed.assessment["_provenance"] == ordinary[0]["_provenance"]


def test_direction_cli_writes_the_same_current_selection_fields(
    rubric, tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (scoring, direction_scoring):
        monkeypatch.setattr(module, "init_db", lambda: None)
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    with factory() as session:
        session.add(
            Report(
                title="Portfolio research",
                abstract=ABSTRACT,
                source="arxiv",
                content_hash="paper",
                analysis_json='{"summary":"keep"}',
            )
        )
        session.commit()
    paper = {"id": 1, "title": "Portfolio research", "abstract": ABSTRACT}
    results = asyncio.run(DirectionFilter(Model()).score_papers([paper]))
    direction_scoring.store_scores(results)
    with factory() as session:
        row = session.get(Report, 1)
        assert row.is_selected and row.selection_relevance == 8
        assert scoring.screening_is_current(row)
        assert (
            json.loads(row.selection_provenance_json)["direction_scores"][0]["score"]
            == 8
        )
        assert json.loads(row.analysis_json)["summary"] == "keep"
    engine.dispose()


def test_old_scoring_result_cannot_overwrite_new_abstract(
    rubric, tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(scoring, "init_db", lambda: None)
    monkeypatch.setattr(scoring, "get_session_factory", lambda: factory)
    paper = {"id": 1, "title": "Portfolio research", "abstract": ABSTRACT}
    results, _ = asyncio.run(scoring.score_papers_batch(Model(), [paper]))
    with factory() as session:
        session.add(
            Report(
                title=paper["title"],
                abstract=ABSTRACT + " Corrected.",
                source="arxiv",
                content_hash="paper",
                selection_relevance=3,
            )
        )
        session.commit()
    stats = scoring.apply_scores(results)
    assert stats["errors"] == 1
    with factory() as session:
        assert session.get(Report, 1).selection_relevance == 3
    engine.dispose()


def test_all_missing_material_records_pending_without_initializing_model(
    rubric, tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (scoring, direction_scoring):
        monkeypatch.setattr(module, "init_db", lambda: None)
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    with factory() as db:
        db.add(
            Report(
                title="Portfolio research",
                source="arxiv",
                content_hash="missing",
                selection_relevance=8,
                selection_score=8,
                is_selected=True,
            )
        )
        db.commit()
    from reportagent.llm import client

    def forbidden():
        raise AssertionError("Missing-material scoring must not initialize a model")

    monkeypatch.setattr(client, "LLMClient", forbidden)
    papers = [{"id": 1, "title": "Portfolio research", "abstract": None}]
    results = asyncio.run(DirectionFilter().score_papers(papers))
    assert results[0].status == "pending_needs_material"
    stats = direction_scoring.store_scores(results)
    assert stats == {"total_scored": 0, "errors": 0}
    with factory() as db:
        row = db.get(Report, 1)
        assert not row.is_selected
        assert row.selection_relevance is None
        assert scoring.screening_is_current(row)
        assert (
            json.loads(row.selection_provenance_json)["status"]
            == "pending_needs_material"
        )
    engine.dispose()
