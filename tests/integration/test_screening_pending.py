"""Insufficient material is pending, never evidence of irrelevance."""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Base, Report
from reportagent.services import paper_scoring as scoring

ABSTRACT = (
    "We study portfolio allocation under transaction costs and estimate an optimal policy. "
    "Our experiments compare its risk and return against a constrained benchmark."
)


class Model:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    async def chat_json(self, messages, **kwargs):
        self.prompts.append(messages)
        return self.response


@pytest.mark.parametrize(
    "abstract", [None, "", r"\\...", "机构：某证券；类型：金融工程；页数：24"]
)
def test_metadata_only_papers_remain_pending_without_model(abstract):
    model = Model([])
    results, failures = asyncio.run(
        scoring.score_papers_batch(
            model, [{"id": 1, "title": "Portfolio optimization", "abstract": abstract}]
        )
    )
    assert not failures
    assert results[0]["status"] == "pending_needs_material"
    assert results[0]["relevance"] is results[0]["score"] is None
    assert model.prompts == []
    stats = scoring.apply_scores(results, dry_run=True)
    assert stats["pending_needs_material"] == 1
    assert stats["total_scored"] == stats["selected"] == stats["rejected"] == 0


def test_pending_partition_keeps_original_indexes_and_model_reported_insufficiency():
    model = Model(
        [
            {
                "index": 1,
                "status": "pending_needs_material",
                "relevance": None,
                "score": None,
                "reason": "Abstract is too ambiguous to judge",
            },
            {"index": 2, "score": 8, "reason": "Research topic matches"},
        ]
    )
    papers = [
        {"id": i, "title": str(i), "abstract": ABSTRACT if i else ""} for i in range(3)
    ]
    results, failures = asyncio.run(scoring.score_papers_batch(model, papers))
    assert not failures
    assert [r["_paper"]["id"] for r in results] == [0, 1, 2]
    assert [r["status"] for r in results] == [
        "pending_needs_material",
        "pending_needs_material",
        "scored",
    ]
    prompt = model.prompts[0][1]["content"]
    assert "论文 #1" in prompt and "论文 #2" in prompt and "论文 #0" not in prompt


def test_pending_persistence_retries_after_material_changes(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(scoring, "init_db", lambda: None)
    monkeypatch.setattr(scoring, "get_session_factory", lambda: factory)
    with factory() as session:
        session.add(
            Report(
                title="Portfolio model",
                content_hash="pending",
                source="bigquant",
                selection_relevance=0,
                selection_score=0,
                selection_reason="Old mistaken rejection",
            )
        )
        session.commit()
        result, _ = asyncio.run(
            scoring.score_papers_batch(Model([]), [session.get(Report, 1)])
        )
    assert scoring.apply_scores(result)["pending_needs_material"] == 1
    with factory() as session:
        row = session.get(Report, 1)
        assert row.selection_relevance is None and row.is_selected is False
        assert (
            json.loads(row.selection_provenance_json)["status"]
            == "pending_needs_material"
        )
        repo = ReportRepository(session)
        assert (
            repo.get_unjudged_reports(scoring_fingerprint=scoring.scoring_fingerprint())
            == []
        )
        row.abstract = ABSTRACT
        session.commit()
        assert [
            p.id
            for p in repo.get_unjudged_reports(
                scoring_fingerprint=scoring.scoring_fingerprint()
            )
        ] == [1]
    engine.dispose()


def test_current_screening_requires_unchanged_rubric_and_material():
    paper = {"title": "Risk model", "abstract": ABSTRACT}
    provenance = {
        "fingerprint": scoring.scoring_fingerprint(),
        "status": "scored",
        "evidence_fingerprint": scoring.evidence_fingerprint(paper),
    }
    paper["selection_provenance_json"] = json.dumps(provenance)
    assert scoring.screening_is_current(paper)
    paper["abstract"] += " Additional empirical evidence."
    assert not scoring.screening_is_current(paper)


def test_direct_apply_cannot_promote_title_only_to_scored():
    stats = scoring.apply_scores(
        [
            {
                "_paper": {"id": 1, "title": "Portfolio model"},
                "score": 9,
                "reason": "Title matches",
            }
        ],
        dry_run=True,
    )
    assert stats["selected"] == stats["total_scored"] == 0
    assert stats["pending_needs_material"] == 1


def test_pending_only_run_needs_no_model_credentials(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(
            Report(
                title="Portfolio model", source="bigquant", content_hash="no-material"
            )
        )
        session.commit()
    monkeypatch.setattr(scoring, "init_db", lambda: None)
    monkeypatch.setattr(scoring, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        scoring, "LLMClient", lambda: pytest.fail("No material needs no model")
    )
    asyncio.run(scoring.score_reports(scoring.ScoringOptions(all_unjudged=True)))
    with factory() as session:
        provenance = json.loads(session.get(Report, 1).selection_provenance_json)
        assert provenance["status"] == "pending_needs_material"
    engine.dispose()
