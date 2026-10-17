"""Offline coverage for collection identity and material persistence boundaries."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.sources.base import deduplicate_results
from reportagent.db.repository import ReportRepository
from reportagent.models.database import Base, Report
from reportagent.models.schemas import (
    AnalysisResult,
    ClassifiedReport,
    ClassificationResult,
    SearchResult,
    SourceType,
)


BODY = "The full paper describes a factor portfolio and its limitations. " * 8


def search_result(**overrides):
    return SearchResult(
        **{
            "title": "Factor portfolio research",
            "authors": ["A. Researcher"],
            "abstract": "A study of factor portfolios.",
            "full_text": BODY,
            "source": SourceType.BIGQUANT,
            "doi": "10.1234/factors",
            **overrides,
        }
    )


def analysis(summary="已生成的研究总结"):
    return AnalysisResult(
        research_question="Which factor portfolio is more robust?",
        core_contribution="An empirical comparison with limitations.",
        summary=summary,
        analyzed_at="2026-10-17T00:00:00+00:00",
    )


def classified(result=None, generated=None):
    return ClassifiedReport(
        search_result=result if result is not None else search_result(),
        classification=ClassificationResult(custom_tags=["factor-study"]),
        analysis=generated,
    )


@pytest.fixture
def library(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        yield session, ReportRepository(session), factory
    engine.dispose()


def test_collection_returns_one_result_for_one_doi():
    result = search_result()
    assert deduplicate_results([result]) == [result]


@pytest.mark.parametrize("identity", ["doi", "arxiv_id", "source_url", "pdf_path"])
def test_collection_keeps_same_title_with_distinct_known_identities(identity):
    first = search_result(**{"doi": None, identity: "first"})
    second = search_result(**{"doi": None, identity: "second"})
    assert deduplicate_results([first, second]) == [first, second]


def test_collection_does_not_merge_unidentified_results_by_title():
    first = search_result(doi=None, authors=["First author"])
    second = search_result(doi=None, authors=["Second author"])
    assert deduplicate_results([first, second]) == [first, second]


@pytest.mark.parametrize("full_text_first", [False, True])
def test_collection_normalizes_doi_and_prefers_complete_material(full_text_first):
    metadata = search_result(
        doi="https://doi.org/10.1234/FACTORS", full_text=None, abstract_only=True
    )
    complete = search_result(pdf_path="/paper.pdf", tables_json='["table"]')
    results = [complete, metadata] if full_text_first else [metadata, complete]
    unique = deduplicate_results(results)
    assert unique == [complete]
    assert unique[0].pdf_path == "/paper.pdf"
    assert unique[0].tables_json == '["table"]'
    assert not unique[0].abstract_only


def test_collection_keeps_arxiv_revisions_separate():
    first = search_result(doi=None, arxiv_id="2610.00001v1")
    second = search_result(doi=None, arxiv_id="2610.00001v2")
    assert deduplicate_results([first, second]) == [first, second]


def test_upsert_persists_supplied_analysis_on_create(library):
    session, repo, factory = library
    generated = analysis()
    action, rid = repo.upsert_report(classified(generated=generated))
    assert action == "added"
    session.expire_all()
    with factory() as fresh_session:
        row = fresh_session.get(Report, rid)
        assert json.loads(row.analysis_json) == generated.model_dump(mode="json")
        assert row.summary == generated.summary


def test_upsert_replaces_supplied_analysis_on_existing_material(library):
    session, repo, _ = library
    _, rid = repo.upsert_report(classified())
    row = session.get(Report, rid)
    row.analysis_json = analysis("Old summary").model_dump_json()
    row.summary = "Old summary"
    session.commit()

    generated = analysis("New summary")
    action, updated = repo.upsert_report(classified(generated=generated))
    assert (action, updated) == ("updated", rid)
    session.expire_all()
    assert json.loads(row.analysis_json) == generated.model_dump(mode="json")
    assert row.summary == generated.summary


@pytest.mark.parametrize("metadata_only", [False, True])
def test_no_analysis_reimport_preserves_current_analysis(library, metadata_only):
    session, repo, _ = library
    _, rid = repo.upsert_report(classified())
    row = session.get(Report, rid)
    saved = analysis().model_dump_json()
    row.analysis_json, row.summary = saved, analysis().summary
    session.commit()

    incoming = (
        search_result(full_text=None, abstract_only=True)
        if metadata_only
        else search_result()
    )
    repo.upsert_report(classified(incoming))
    session.expire_all()
    assert row.analysis_json == saved
    assert row.summary == analysis().summary
    assert row.full_text == BODY


@pytest.mark.parametrize("supply_analysis", [False, True])
@pytest.mark.parametrize(
    "changed",
    [
        {"title": "Corrected title"},
        {"abstract": "Corrected abstract"},
        {"full_text": BODY + "New experimental evidence."},
        {"pdf_path": "/reparsed.pdf", "tables_json": '["new table"]'},
    ],
    ids=["title", "abstract", "full-text", "parser-artifacts"],
)
def test_changed_material_invalidates_old_outputs_before_saving_new_analysis(
    library, supply_analysis, changed
):
    session, repo, _ = library
    _, rid = repo.upsert_report(classified())
    row = session.get(Report, rid)
    row.analysis_json = row.evaluation_json = row.knowledge_card_json = '{"old": true}'
    row.summary = "Old summary"
    session.commit()

    generated = analysis("Analysis of the updated paper") if supply_analysis else None
    action, updated = repo.upsert_report(
        classified(search_result(**changed), generated)
    )
    assert (action, updated) == ("updated", rid)
    session.expire_all()
    assert row.evaluation_json is None
    assert row.knowledge_card_json is None
    if generated is None:
        assert row.analysis_json is None
        assert row.summary is None
    else:
        assert json.loads(row.analysis_json) == generated.model_dump(mode="json")
        assert row.summary == generated.summary


@pytest.mark.parametrize("existing", [False, True])
def test_uncommitted_analysis_can_be_rolled_back(library, existing):
    session, repo, factory = library
    old_json = analysis("Old summary").model_dump_json()
    if existing:
        _, rid = repo.upsert_report(classified())
        row = session.get(Report, rid)
        row.analysis_json, row.summary = old_json, "Old summary"
        session.commit()

    generated = analysis("Uncommitted summary")
    _, rid = repo.upsert_report(classified(generated=generated), commit=False)
    row = session.get(Report, rid)
    assert json.loads(row.analysis_json) == generated.model_dump(mode="json")
    assert row.summary == generated.summary
    with factory() as other_session:
        previous = other_session.get(Report, rid)
        if existing:
            assert previous.analysis_json == old_json
            assert previous.summary == "Old summary"
        else:
            assert previous is None
    session.rollback()
    row = session.get(Report, rid)
    if existing:
        assert row.analysis_json == old_json
        assert row.summary == "Old summary"
    else:
        assert row is None


@pytest.mark.parametrize("older_id", ["2610.00001v1", "2610.00001"])
def test_delayed_arxiv_analysis_cannot_overwrite_newer_revision(library, older_id):
    session, repo, _ = library
    current = search_result(source=SourceType.ARXIV, arxiv_id="2610.00001v2")
    _, rid = repo.upsert_report(classified(current))
    row = session.get(Report, rid)
    saved = analysis("Current revision summary").model_dump_json()
    row.analysis_json, row.summary = saved, "Current revision summary"
    row.evaluation_json = '{"current": true}'
    session.commit()

    older = search_result(
        source=SourceType.ARXIV,
        arxiv_id=older_id,
        title="Old revision title",
        full_text="Outdated body",
    )
    action, updated = repo.upsert_report(classified(older, analysis("Stale analysis")))
    assert (action, updated) == ("updated", rid)
    session.expire_all()
    assert row.arxiv_id == "2610.00001v2"
    assert row.title == current.title
    assert row.full_text == BODY
    assert row.analysis_json == saved
    assert row.summary == "Current revision summary"
    assert row.evaluation_json == '{"current": true}'


@pytest.mark.parametrize("supply_analysis", [False, True])
def test_new_arxiv_revision_replaces_analysis_after_invalidation(
    library, supply_analysis
):
    session, repo, _ = library
    original = search_result(source=SourceType.ARXIV, arxiv_id="2610.00001v1")
    _, rid = repo.upsert_report(classified(original))
    row = session.get(Report, rid)
    row.analysis_json = row.evaluation_json = row.knowledge_card_json = '{"old": true}'
    row.summary = "Old revision summary"
    session.commit()

    generated = analysis("New revision summary") if supply_analysis else None
    newer = original.model_copy(update={"arxiv_id": "2610.00001v2"})
    _, updated = repo.upsert_report(classified(newer, generated))
    session.expire_all()
    assert updated == rid
    assert row.arxiv_id == "2610.00001v2"
    assert row.evaluation_json is None
    assert row.knowledge_card_json is None
    if generated is None:
        assert row.analysis_json is None
        assert row.summary is None
    else:
        assert json.loads(row.analysis_json) == generated.model_dump(mode="json")
        assert row.summary == generated.summary


def test_batch_upsert_retains_each_generated_analysis(library):
    session, repo, _ = library
    reports = [
        classified(search_result(doi=f"10.1234/paper-{i}"), analysis(f"Summary {i}"))
        for i in range(2)
    ]
    stored = repo.batch_upsert(reports)
    assert stored.newly_added == 2
    assert stored.errors == []
    session.expire_all()
    for rid, incoming in zip(stored.report_ids, reports, strict=True):
        row = session.get(Report, rid)
        assert json.loads(row.analysis_json) == incoming.analysis.model_dump(
            mode="json"
        )
        assert row.summary == incoming.analysis.summary
