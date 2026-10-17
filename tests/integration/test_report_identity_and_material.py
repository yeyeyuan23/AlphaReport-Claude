"""Offline regressions for identity and coherent material replacement."""

import json

import pytest
from sqlalchemy import create_engine

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Report
from reportagent.models.schemas import (
    ClassifiedReport,
    ClassificationResult,
    SearchResult,
    SourceType,
)
from reportagent.utils.hashing import content_hash


@pytest.fixture
def library(memory_session_factory):
    with memory_session_factory(expire_on_commit=True) as session:
        yield session, ReportRepository(session)


def incoming(**values):
    return ClassifiedReport(
        search_result=SearchResult(
            **{
                "title": "Same title",
                "authors": ["A"],
                "source": SourceType.BIGQUANT,
                "full_text": "First body",
                **values,
            }
        ),
        classification=ClassificationResult(),
    )


@pytest.mark.parametrize("identity", ["doi", "source_url", "source_id", "arxiv_id"])
def test_distinct_identifiers_never_merge_via_title(library, identity):
    session, repo = library

    def item(n):
        values = {"full_text": f"Body {n}"}
        if identity == "source_id":
            values["raw_metadata"] = {"info_code": f"code-{n}"}
        elif identity == "doi":
            values.update(
                doi=f"10.1234/paper{n}", source_url=f"https://papers.test/{n}"
            )
        else:
            values[identity] = (
                f"2005.1366{n}v1"
                if identity == "arxiv_id"
                else f"https://papers.test/{n}"
            )
        return incoming(**values)

    _, first = repo.upsert_report(item(1))
    _, second = repo.upsert_report(item(2))
    assert first != second
    assert session.query(Report).count() == 2
    for n, rid in [(1, first), (2, second)]:
        row = session.get(Report, rid)
        assert row.full_text == f"Body {n}"
        assert row.source_url == item(n).search_result.source_url


@pytest.mark.parametrize("legacy", [False, True])
def test_normalized_doi_matches_despite_changed_title_and_source(library, legacy):
    session, repo = library
    _, first = repo.upsert_report(
        incoming(
            doi="https://doi.org/10.1234/ABC", source_url="https://papers.test/old"
        )
    )
    if legacy:
        session.get(Report, first).content_hash = content_hash("Same title", ["A"])
        session.commit()
    _, updated = repo.upsert_report(
        incoming(
            title="Corrected title",
            authors=["B"],
            doi="doi:10.1234/abc",
            source_url="https://papers.test/new",
            full_text="New body",
        )
    )
    assert updated == first
    assert session.query(Report).count() == 1
    row = session.get(Report, first)
    assert row.title == "Corrected title" and row.full_text == "New body"
    assert row.source_url == "https://papers.test/new"


def test_conflicting_doi_does_not_fall_back_to_same_url(library):
    session, repo = library
    _, first = repo.upsert_report(
        incoming(doi="10.1234/a", source_url="https://papers.test/item")
    )
    _, second = repo.upsert_report(
        incoming(
            doi="10.1234/b", source_url="https://papers.test/item", full_text="Second"
        )
    )
    assert first != second
    assert session.get(Report, first).full_text == "First body"


def test_legacy_weak_identity_is_reusable_but_not_across_sources(library):
    session, repo = library
    row = Report(
        title="Same title",
        authors=json.dumps(["A"]),
        source="local_pdf",
        content_hash=content_hash("Same title", ["A"]),
    )
    session.add(row)
    session.commit()
    _, same = repo.upsert_report(incoming(source=SourceType.LOCAL_PDF))
    assert same == row.id
    _, different = repo.upsert_report(incoming())
    assert different != same


def test_source_url_identity_survives_title_correction(library):
    _, repo = library
    _, first = repo.upsert_report(incoming(source_url="https://PAPERS.test/item#old"))
    _, second = repo.upsert_report(
        incoming(title="Corrected", source_url="https://papers.test/item#new")
    )
    assert first == second


def test_reimport_replaces_material_bundle_and_invalidates_old_evaluation(library):
    session, repo = library
    _, rid = repo.upsert_report(
        incoming(
            source=SourceType.LOCAL_PDF,
            pdf_path="/old.pdf",
            tables_json='["old table"]',
            equations_json='["old equation"]',
        )
    )
    row = session.get(Report, rid)
    row.evaluation_json = row.analysis_json = '{"old": true}'
    session.commit()
    _, updated = repo.upsert_report(
        incoming(
            source=SourceType.LOCAL_PDF,
            pdf_path="/moved/new.pdf",
            full_text="Updated body",
            tables_json='["new table"]',
        )
    )
    assert updated == rid
    assert row.pdf_path == "/moved/new.pdf" and row.full_text == "Updated body"
    assert row.tables_json == '["new table"]' and row.equations_json is None
    assert row.evaluation_json is row.analysis_json is None


def test_same_text_new_parser_artifacts_replace_and_invalidate(library):
    session, repo = library
    _, rid = repo.upsert_report(
        incoming(
            source=SourceType.LOCAL_PDF, pdf_path="/old.pdf", tables_json='["old"]'
        )
    )
    row = session.get(Report, rid)
    row.evaluation_json = '{"old":true}'
    session.commit()
    repo.upsert_report(incoming(source=SourceType.LOCAL_PDF, pdf_path="/new.pdf"))
    assert row.pdf_path == "/new.pdf" and row.tables_json is None
    assert row.evaluation_json is None


def test_metadata_only_refresh_keeps_existing_complete_material(library):
    session, repo = library
    _, rid = repo.upsert_report(
        incoming(
            source_url="https://papers.test/item",
            pdf_path="/cached.pdf",
            tables_json='["table"]',
        )
    )
    repo.upsert_report(
        incoming(
            source_url="https://papers.test/item", full_text=None, abstract_only=True
        )
    )
    row = session.get(Report, rid)
    assert (
        row.full_text == "First body"
        and row.pdf_path == "/cached.pdf"
        and row.tables_json == '["table"]'
    )


@pytest.mark.parametrize("legacy_hash_only", [False, True])
def test_source_id_survives_url_and_title_changes(library, legacy_hash_only):
    session, repo = library
    _, rid = repo.upsert_report(
        incoming(
            raw_metadata={"info_code": "stable-1"},
            source_url="https://papers.test/old",
            pdf_path="/old.pdf",
            tables_json='["old"]',
        )
    )
    if legacy_hash_only:
        session.get(Report, rid).source_item_id = None
        session.commit()
    _, updated = repo.upsert_report(
        incoming(
            title="Corrected title",
            raw_metadata={"info_code": "stable-1"},
            source_url="https://papers.test/new",
            full_text="New body",
            pdf_path="/new.pdf",
            tables_json='["new"]',
            equations_json='["new eq"]',
        )
    )
    assert updated == rid
    assert session.query(Report).count() == 1
    row = session.get(Report, rid)
    assert row.source_item_id == "bigquant:stable-1"
    assert (
        row.title,
        row.source_url,
        row.full_text,
        row.pdf_path,
        row.tables_json,
        row.equations_json,
    ) == (
        "Corrected title",
        "https://papers.test/new",
        "New body",
        "/new.pdf",
        '["new"]',
        '["new eq"]',
    )


def test_different_source_ids_cannot_merge_through_common_url(library):
    session, repo = library
    ids = []
    for n in (1, 2):
        _, rid = repo.upsert_report(
            incoming(
                raw_metadata={"info_code": f"item-{n}"},
                source_url="https://papers.test/common",
                full_text=f"Body {n}",
                pdf_path=f"/paper-{n}.pdf",
                tables_json=json.dumps([f"table-{n}"]),
                equations_json=json.dumps([f"equation-{n}"]),
            )
        )
        ids.append(rid)
    assert ids[0] != ids[1]
    for n, rid in enumerate(ids, 1):
        row = session.get(Report, rid)
        assert row.source_item_id == f"bigquant:item-{n}"
        assert (row.full_text, row.pdf_path, row.tables_json, row.equations_json) == (
            f"Body {n}",
            f"/paper-{n}.pdf",
            json.dumps([f"table-{n}"]),
            json.dumps([f"equation-{n}"]),
        )


def test_source_id_remains_findable_with_doi_based_content_hash(library):
    session, repo = library
    _, rid = repo.upsert_report(
        incoming(
            raw_metadata={"source_id": "stable-1"},
            source_url="https://papers.test/item",
            doi="10.1234/discovered",
        )
    )
    _, updated = repo.upsert_report(
        incoming(
            title="New title",
            raw_metadata={"source_id": "stable-1"},
            source_url="https://papers.test/new",
        )
    )
    assert updated == rid
    assert session.query(Report).count() == 1


def test_source_identity_migration_is_additive_and_repeatable(monkeypatch):
    from reportagent.db import engine as db_engine

    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr(db_engine, "get_engine", lambda: engine)
    try:
        with engine.begin() as db:
            db.exec_driver_sql(
                "CREATE TABLE reports (id INTEGER PRIMARY KEY, title TEXT, source TEXT, content_hash TEXT)"
            )
            db.exec_driver_sql(
                "INSERT INTO reports VALUES (1, 'Legacy', 'bigquant', 'legacy-hash')"
            )
        db_engine.init_db()
        db_engine.init_db()
        with engine.connect() as db:
            columns = {
                row[1] for row in db.exec_driver_sql("PRAGMA table_info(reports)")
            }
            assert "source_item_id" in columns
            assert db.exec_driver_sql(
                "SELECT title, content_hash, source_item_id FROM reports"
            ).one() == ("Legacy", "legacy-hash", None)
    finally:
        engine.dispose()


def test_bigquant_relative_and_absolute_doc_paths_identify_same_document(library):
    session, repo = library
    _, rid = repo.upsert_report(
        incoming(
            raw_metadata={"doc_path": "/wiki/doc/stable-item"},
            source_url="https://bigquant.com/wiki/doc/stable-item",
        )
    )
    _, updated = repo.upsert_report(
        incoming(
            title="Hydrated title",
            raw_metadata={"doc_path": "https://bigquant.com/wiki/doc/stable-item"},
            source_url="https://bigquant.com/wiki/doc/stable-item",
            full_text="Hydrated body",
        )
    )
    assert updated == rid
    assert session.query(Report).count() == 1
    assert session.get(Report, rid).full_text == "Hydrated body"
