"""Offline cache policy regressions across daily and explicit paper reviews."""

from reportagent.sources.date_window import source_date


import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from reportagent.models.database import Report
from reportagent.models.schemas import SearchResult, SourceType
from reportagent.services import one_report, report_quality as quality
from tests.support.report_fixtures import TEXT, complete_analysis, response


@pytest.fixture
def prepared_cache(tmp_path, monkeypatch, memory_session_factory):
    factory = memory_session_factory
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF fixture")
    paper = SearchResult(
        title="Portfolio research",
        abstract="Portfolio methods",
        authors=["A"],
        source=SourceType.ARXIV,
        arxiv_id="2601.12345v1",
        full_text=TEXT,
        abstract_only=False,
        pdf_path=str(pdf),
        source_url="https://arxiv.org/abs/2601.12345v1",
    )
    monkeypatch.setattr(one_report, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(one_report, "init_db", lambda: None)
    monkeypatch.setattr(one_report, "get_session_factory", lambda: factory)
    monkeypatch.setattr(one_report, "collect_paper", AsyncMock(return_value=paper))
    monkeypatch.setattr(one_report, "analysis_fingerprint", lambda: "policy")
    monkeypatch.setattr(one_report, "get_llm_model", lambda: "fixture-model")
    rid = one_report.store_paper(paper)
    review = {
        **response(),
        "schema_version": quality.SCHEMA_VERSION,
        "status": "reviewed",
        "tier": "browse",
        "label": "值得浏览",
        "model": "fixture-model",
        "text_complete": True,
        "fingerprint": quality.review_fingerprint(
            one_report._stored_review_input(rid), "fixture-model"
        ),
    }
    artifact = {
        "analysis": complete_analysis(),
        "analysis_fingerprint": "policy",
        "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "evaluation": review,
        "model": "fixture-model",
        "deliveries": [{"message_id": "delivered"}],
    }
    path = tmp_path / "data/one_reports" / f"{paper.arxiv_id}.json"
    one_report.save_artifact(path, artifact)
    return factory, paper, rid, path, artifact


def test_single_report_prefers_newer_canonical_database_review(
    prepared_cache, monkeypatch
):
    factory, paper, rid, path, artifact = prepared_cache
    current = {
        **artifact["evaluation"],
        "tier": "exclude",
        "reading_advice": "New daily review",
    }
    with factory() as session:
        session.get(Report, rid).evaluation_json = json.dumps(current)
        session.commit()
    monkeypatch.setattr(
        one_report, "LLMClient", lambda **_: pytest.fail("Unexpected model")
    )
    result = asyncio.run(one_report.prepare_report(paper.arxiv_id))[1]
    assert result["evaluation"] == current
    assert "New daily review" in result["brief"]
    assert result["deliveries"] == artifact["deliveries"]
    with factory() as session:
        assert json.loads(session.get(Report, rid).evaluation_json) == current


@pytest.mark.parametrize(
    "invalid",
    [
        {"schema_version": 0},
        {"model": "obsolete"},
        {"text_complete": False},
        {"model": None},
        {"schema_version": None},
        {"fingerprint": "obsolete-input"},
    ],
)
def test_single_report_refreshes_invalid_review_without_repeating_analysis(
    prepared_cache, monkeypatch, invalid
):
    factory, paper, rid, path, artifact = prepared_cache
    current = dict(artifact["evaluation"])
    artifact["evaluation"].update(invalid)
    one_report.save_artifact(path, artifact)
    llm = MagicMock(model="fixture-model", thinking="disabled", calls=[])
    llm._client.close = AsyncMock()
    monkeypatch.setattr(one_report, "LLMClient", lambda **_: llm)
    monkeypatch.setattr(
        one_report, "AnalysisAgent", lambda **_: pytest.fail("Repeated phases")
    )
    review = AsyncMock(return_value=current)
    monkeypatch.setattr(quality, "evaluate_paper", review)
    result = asyncio.run(one_report.prepare_report(paper.arxiv_id))[1]
    review.assert_awaited_once()
    assert result["evaluation"] == current
    assert result["analysis"] == artifact["analysis"]
    assert result["deliveries"] == artifact["deliveries"]


def test_cached_revision_parse_uses_timeout_and_stage_diagnostics(
    tmp_path, monkeypatch, memory_session_factory, caplog
):
    import threading
    from reportagent.sources import content_fetch

    factory = memory_session_factory
    arxiv_id = "2601.12345v1"
    pdf = tmp_path / "data/pdfs" / f"{arxiv_id}.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF cached")
    with factory() as session:
        session.add(
            Report(
                title="Cached revision",
                content_hash="cached",
                source="arxiv",
                arxiv_id=arxiv_id,
                authors='["A"]',
                source_url=f"https://arxiv.org/abs/{arxiv_id}",
            )
        )
        session.commit()
    monkeypatch.setattr(one_report, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(one_report, "get_db_path", lambda: pdf)
    monkeypatch.setattr(one_report, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        content_fetch,
        "get_pdf_policy",
        lambda: content_fetch.PDFPolicy(parse_timeout_seconds=0.01),
    )
    release = threading.Event()

    def blocked_parse(_):
        release.wait(timeout=1)
        return TEXT

    monkeypatch.setattr(one_report.ArxivSource, "_parse_pdf_bytes", blocked_parse)

    async def check():
        try:
            # The outer bound catches the original unbounded cached parse path.
            with pytest.raises(
                content_fetch.PDFTransferError, match="parse.*TimeoutError"
            ):
                await asyncio.wait_for(one_report.collect_paper(arxiv_id), timeout=0.1)
        finally:
            release.set()

    asyncio.run(check())
    assert '"stage": "parse"' in caplog.text
    assert '"status": "failed"' in caplog.text


@pytest.mark.parametrize("review_source", ["database", "artifact"])
def test_new_phase_analysis_reuses_current_review(
    prepared_cache, monkeypatch, review_source
):
    from reportagent.models.schemas import AnalysisResult

    factory, paper, rid, path, artifact = prepared_cache
    canonical = {**artifact["evaluation"], "reading_advice": "Current daily conclusion"}
    if review_source == "database":
        with factory() as session:
            session.get(Report, rid).evaluation_json = json.dumps(canonical)
            session.commit()
    else:
        artifact["evaluation"] = canonical
    artifact["analysis_fingerprint"] = "obsolete-phase-prompt"
    one_report.save_artifact(path, artifact)
    llm = MagicMock(model="fixture-model", thinking="disabled", calls=[])
    llm._client.close = AsyncMock()
    agent = MagicMock(_config={"analysis": {"phases": {}}})
    agent.analyze_report_directly = AsyncMock(
        return_value=AnalysisResult.model_validate(complete_analysis())
    )
    monkeypatch.setattr(one_report, "LLMClient", lambda **_: llm)
    monkeypatch.setattr(one_report, "AnalysisAgent", lambda **_: agent)
    review = AsyncMock(
        side_effect=AssertionError("Valid canonical review was repeated")
    )
    monkeypatch.setattr(quality, "evaluate_paper", review)
    result = asyncio.run(one_report.prepare_report(paper.arxiv_id))[1]
    agent.analyze_report_directly.assert_awaited_once()
    assert result["evaluation"] == canonical
    review.assert_not_awaited()


@pytest.mark.parametrize("revision", ["2601.12345v1", "2601.12345v2", "2601.12345v3"])
def test_storing_paper_preserves_known_date_across_revision_policy(
    prepared_cache, revision
):
    from datetime import datetime

    factory, paper, rid, path, artifact = prepared_cache
    known = datetime(2020, 5, 27)
    one_report.store_paper(
        paper.model_copy(update={"arxiv_id": "2601.12345v2", "published_date": known})
    )
    one_report.store_paper(
        paper.model_copy(update={"arxiv_id": revision, "published_date": None})
    )
    with factory() as session:
        row = session.get(Report, rid)
        assert row.arxiv_id == revision
        assert row.published_date == source_date(known)


def test_single_automatic_classification_uses_canonical_abstract_material(
    prepared_cache,
):
    factory, paper, rid, path, artifact = prepared_cache
    background = (
        "machine learning deep learning neural network transformer GNN LSTM reinforcement learning GPT LLM "
        * 20
    )
    one_report.store_paper(
        paper.model_copy(
            update={
                "title": "A neutral research paper",
                "abstract": "We examine research decisions.",
                "full_text": background,
            }
        )
    )
    with factory() as session:
        assert session.get(Report, rid).topics == ""


def test_single_phase_analysis_without_review_does_not_cache_legacy_ready_card(
    prepared_cache,
):
    from reportagent.api import library
    from reportagent.models.schemas import AnalysisResult

    factory, paper, rid, path, artifact = prepared_cache
    result = AnalysisResult.model_validate(complete_analysis())
    one_report.store_paper(paper, result)
    with factory() as session:
        row = session.get(Report, rid)
        assert row.knowledge_card_json is None
        card = library.get_knowledge_card(rid, force=False, db=session)["data"]
        assert card["status"] == "pending"
        assert card["highlights"] == []
        assert card["summary"] == paper.abstract


def test_single_stored_card_matches_api_projection_of_current_review(
    prepared_cache, monkeypatch
):
    from reportagent.api import library
    from reportagent.models.schemas import AnalysisResult
    from reportagent.services import knowledge_cards

    factory, paper, rid, path, artifact = prepared_cache
    monkeypatch.setattr(knowledge_cards, "get_llm_model", lambda: "fixture-model")
    evaluation = artifact["evaluation"]
    one_report.store_paper(
        paper, AnalysisResult.model_validate(complete_analysis()), evaluation
    )
    with factory() as session:
        row = session.get(Report, rid)
        saved_card = json.loads(row.knowledge_card_json)
        assert saved_card["status"] == "ready"
        assert saved_card["summary"] == evaluation["research_question"]
        assert saved_card["highlights"] == [e["claim"] for e in evaluation["findings"]]
        assert (
            library.get_knowledge_card(rid, force=False, db=session)["data"]
            == saved_card
        )


def test_single_refresh_invalidates_classification_when_fallback_introduction_changes(
    prepared_cache,
):
    factory, paper, _, path, artifact = prepared_cache
    incoming = paper.model_copy(
        update={
            "arxiv_id": "2601.99999v1",
            "title": "A neutral research paper",
            "abstract": "",
            "full_text": "Introduction\nWe investigate basic evidence.\nMethods\n"
            + TEXT,
        }
    )
    rid = one_report.store_paper(incoming)
    with factory() as session:
        row = session.get(Report, rid)
        row.classification_method = "hybrid"
        row.classification_provenance_json = '{"input":"prior introduction"}'
        session.commit()
    new_intro = (
        "machine learning deep learning neural network transformer GNN LSTM reinforcement learning GPT LLM "
        * 20
    )
    one_report.store_paper(
        incoming.model_copy(
            update={
                "full_text": "Introduction\n" + new_intro + "\nMethods\n" + TEXT,
            }
        )
    )
    with factory() as session:
        row = session.get(Report, rid)
        assert row.classification_method == "rule"
        assert row.classification_provenance_json is None
        assert row.topics == "ai_ml_model"


def test_single_missing_abstract_retains_classification_of_preserved_abstract(
    prepared_cache,
):
    factory, paper, rid, path, artifact = prepared_cache
    with factory() as session:
        row = session.get(Report, rid)
        row.classification_method = "hybrid"
        row.classification_provenance_json = '{"input":"preserved abstract"}'
        session.commit()
    one_report.store_paper(
        paper.model_copy(
            update={
                "abstract": "",
                "full_text": "Introduction\nA different introductory context.\nMethods\n"
                + TEXT,
            }
        )
    )
    with factory() as session:
        row = session.get(Report, rid)
        assert row.abstract == paper.abstract
        assert row.classification_method == "hybrid"
        assert row.classification_provenance_json == '{"input":"preserved abstract"}'


def test_single_metadata_only_refresh_retains_classification_of_preserved_introduction(
    prepared_cache,
):
    factory, paper, _, path, artifact = prepared_cache
    incoming = paper.model_copy(
        update={
            "arxiv_id": "2601.99999v1",
            "title": "A neutral research paper",
            "abstract": "",
            "full_text": "Introduction\nOriginal research context.\nMethods\n" + TEXT,
        }
    )
    rid = one_report.store_paper(incoming)
    with factory() as session:
        row = session.get(Report, rid)
        row.classification_method = "hybrid"
        session.commit()
    one_report.store_paper(
        incoming.model_copy(update={"full_text": "", "abstract_only": True})
    )
    with factory() as session:
        row = session.get(Report, rid)
        assert row.full_text == incoming.full_text
        assert row.classification_method == "hybrid"
