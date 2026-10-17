"""SQLite regressions for metadata screening followed by material hydration."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Base, Report
from reportagent.models.schemas import ClassifiedReport, ClassificationResult, SearchResult, SourceType
from reportagent.services.paper_scoring import evidence_fingerprint, scoring_fingerprint, screening_is_current


@pytest.fixture
def library(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'library.sqlite'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session, ReportRepository(session)
    engine.dispose()


def incoming(**overrides):
    values = {
        "title": "Portfolio risk forecasting", "authors": ["A"],
        "abstract": "We forecast portfolio downside risk using conditional quantile models.",
        "source": SourceType.ARXIV, "arxiv_id": "2601.00001v1",
        "source_url": "https://arxiv.org/abs/2601.00001v1", **overrides,
    }
    return ClassifiedReport(search_result=SearchResult(**values), classification=ClassificationResult())


def seed_reviewed(library, **overrides):
    session, repo = library
    _, rid = repo.upsert_report(incoming(**overrides))
    row = session.get(Report, rid)
    provenance = json.dumps({"status": "scored", "fingerprint": scoring_fingerprint(),
                             "evidence_fingerprint": evidence_fingerprint(row)})
    repo.update_selection(rid, 8.4, "Relevant portfolio risk research", True,
                          direction="risk_model", relevance=8.4, provenance_json=provenance)
    row.analysis_json = '{"analysis":"saved"}'
    row.evaluation_json = '{"evaluation":"saved"}'
    row.knowledge_card_json = '{"card":"saved"}'
    row.summary = "Saved summary"
    session.commit()
    return rid, provenance


def persisted(library, rid):
    session, _ = library
    session.expire_all()
    return session.get(Report, rid)


def assert_screening_retained(row, provenance):
    assert (row.selection_score, row.selection_reason, row.is_selected,
            row.selection_direction, row.selection_relevance, row.selection_provenance_json) == (
        8.4, "Relevant portfolio risk research", True, "risk_model", 8.4, provenance)
    assert screening_is_current(row)


def assert_review_cleared(row):
    assert (row.analysis_json, row.evaluation_json, row.knowledge_card_json, row.summary) == (None,) * 4


def test_same_revision_pdf_hydration_preserves_abstract_screening(library):
    rid, provenance = seed_reviewed(library, full_text="Abstract preview", abstract_only=True)
    _, repo = library
    action, updated = repo.upsert_report(incoming(full_text="Complete PDF body", pdf_path="/library/paper.pdf",
        tables_json='["PDF table"]', equations_json='["PDF equation"]'))
    row = persisted(library, rid)
    assert (action, updated) == ("updated", rid)
    assert (row.full_text, row.has_full_text, row.pdf_path, row.tables_json, row.equations_json) == (
        "Complete PDF body", True, "/library/paper.pdf", '["PDF table"]', '["PDF equation"]')
    assert_screening_retained(row, provenance)
    assert_review_cleared(row)


@pytest.mark.parametrize("changed", [
    {"full_text": "Reparsed PDF body"}, {"pdf_path": "/library/moved.pdf"},
    {"tables_json": '["reparsed table"]'}, {"equations_json": '["reparsed equation"]'},
])
def test_same_revision_material_changes_clear_review_but_preserve_screening(library, changed):
    material = {"full_text": "PDF body", "pdf_path": "/library/paper.pdf",
                "tables_json": '["table"]', "equations_json": '["equation"]'}
    rid, provenance = seed_reviewed(library, **material)
    _, repo = library
    repo.upsert_report(incoming(**{**material, **changed}))
    row = persisted(library, rid)
    assert_screening_retained(row, provenance)
    assert_review_cleared(row)
    for field, value in changed.items():
        assert getattr(row, field) == value


@pytest.mark.parametrize("refresh", [
    {"full_text": None, "abstract_only": True, "abstract": None},
    {"full_text": "HTML preview", "abstract_only": True},
])
def test_same_revision_metadata_or_preview_refresh_preserves_pdf_and_review(library, refresh):
    rid, provenance = seed_reviewed(library, full_text="Complete PDF body", pdf_path="/library/paper.pdf",
        tables_json='["PDF table"]', equations_json='["PDF equation"]')
    _, repo = library
    repo.upsert_report(incoming(**refresh))
    row = persisted(library, rid)
    assert (row.full_text, row.has_full_text, row.pdf_path, row.tables_json, row.equations_json) == (
        "Complete PDF body", True, "/library/paper.pdf", '["PDF table"]', '["PDF equation"]')
    assert_screening_retained(row, provenance)
    assert (row.analysis_json, row.evaluation_json, row.knowledge_card_json, row.summary) == (
        '{"analysis":"saved"}', '{"evaluation":"saved"}', '{"card":"saved"}', "Saved summary")


@pytest.mark.parametrize("change", [
    {"title": "Corrected portfolio risk title"},
    {"abstract": "A corrected abstract with different evidence about portfolio forecasting."},
    {"arxiv_id": "2601.00001v2"},
])
def test_changed_screening_metadata_or_revision_invalidates_screening_and_review(library, change):
    rid, _ = seed_reviewed(library, full_text="PDF body", pdf_path="/library/paper.pdf")
    _, repo = library
    _, updated = repo.upsert_report(incoming(**change))
    row = persisted(library, rid)
    assert updated == rid
    assert (row.selection_score, row.selection_reason, row.is_selected, row.selection_direction,
            row.selection_relevance, row.selection_provenance_json) == (0.0, None, False, None, None, None)
    assert not screening_is_current(row)
    assert_review_cleared(row)


@pytest.mark.parametrize("checked", [False, True])
def test_local_abstract_removal_invalidates_only_after_explicit_check(library, checked):
    rid, provenance = seed_reviewed(library, source=SourceType.LOCAL_PDF, full_text="PDF body",
        pdf_path="/library/paper.pdf")
    _, repo = library
    repo.upsert_report(incoming(source=SourceType.LOCAL_PDF, full_text="PDF body",
        pdf_path="/library/paper.pdf", abstract=None,
        raw_metadata={"abstract_source": "unavailable"} if checked else {}))
    row = persisted(library, rid)
    if checked:
        assert row.abstract is None
        assert row.selection_provenance_json is None
        assert row.selection_score == 0.0 and not row.is_selected
        assert_review_cleared(row)
    else:
        assert row.abstract == "We forecast portfolio downside risk using conditional quantile models."
        assert_screening_retained(row, provenance)
        assert row.summary == "Saved summary"
