from reportagent.sources.date_window import source_date
import json
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from reportagent.models.schemas import (
    SearchResult,
    SourceType,
    ClassificationResult,
    ClassifiedReport,
)
from reportagent.db.repository import ReportRepository
from reportagent.processors.metadata_extractor import MetadataExtractor
from reportagent.processors.pdf_extractor import PDFContent
from reportagent.services import daily_push


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Securities Research Report\nPublished: 2024-02-03", "broker"),
        ("证券研究报告\n发布日期：2024年2月3日", "broker"),
        ("arXiv:2402.00001v1 [q-fin.ST] 3 Feb 2024", "academic"),
        ("Published as a conference paper at ICLR 2024", "academic"),
        ("BigQuant 社区研究", "community"),
        ("Unlabelled document", "unknown"),
        (
            "Unlabelled document\nAbstract\nWe cite a securities research report and arXiv:2402.00001.",
            "unknown",
        ),
    ],
)
def test_material_type_uses_document_evidence(header, expected):
    result = MetadataExtractor().extract(PDFContent(text=header, page_count=1))
    assert result.get("material_type") == expected
    assert bool(result.get("material_type_info", {}).get("evidence")) == (
        expected != "unknown"
    )


@pytest.fixture
def factory(memory_session_factory):
    return memory_session_factory


def incoming(**updates):
    args = dict(
        title="Portfolio metadata fixture",
        authors=["Fixture"],
        source=SourceType.LOCAL_PDF,
        abstract="We study portfolio risk and stock returns.",
        raw_metadata={
            "publication_date_checked": True,
            "publication_date_info": {"precision": "unknown", "source": "unknown"},
            "material_type": "unknown",
            "material_type_info": {"source": "unknown"},
        },
    )
    args.update(updates)
    return ClassifiedReport(
        search_result=SearchResult(**args), classification=ClassificationResult()
    )


def test_repository_persists_material_metadata_without_changing_source(factory):
    sr = incoming(
        published_date=datetime(2024, 2, 3),
        raw_metadata={
            "publication_date_info": {
                "precision": "day",
                "year": 2024,
                "month": 2,
                "day": 3,
                "source": "labelled_publication_date",
                "evidence": "Published: 2024-02-03",
            },
            "material_type": "broker",
            "material_type_info": {
                "source": "document_header",
                "evidence": "证券研究报告",
            },
        },
    )
    with factory() as session:
        _, rid = ReportRepository(session).upsert_report(sr)
        row = session.get(Report, rid)
        assert row.source == "local_pdf"
        assert getattr(row, "material_type", None) == "broker"
        assert json.loads(row.material_type_info_json)["evidence"] == "证券研究报告"
        assert row.published_date == source_date(datetime(2024, 2, 3))


def test_inconclusive_reimport_preserves_supported_date_and_material_type(factory):
    with factory() as session:
        repo = ReportRepository(session)
        first = incoming(
            published_date=datetime(2024, 2, 3),
            raw_metadata={
                "publication_date_checked": True,
                "publication_date_info": {
                    "precision": "day",
                    "year": 2024,
                    "month": 2,
                    "day": 3,
                    "source": "labelled_publication_date",
                    "evidence": "Published: 2024-02-03",
                },
                "material_type": "academic",
                "material_type_info": {
                    "source": "document_header",
                    "evidence": "arXiv:2402.00001",
                },
            },
        )
        _, rid = repo.upsert_report(first)
        repo.upsert_report(incoming())
        row = session.get(Report, rid)
        assert row.published_date == source_date(datetime(2024, 2, 3))
        assert getattr(row, "material_type", None) == "academic"


@pytest.mark.parametrize("explicit", [False, True])
def test_old_and_partial_dates_stay_in_library_but_not_daily(factory, explicit):
    with factory() as session:
        rows = [
            Report(
                id=i,
                title=f"Document {i}",
                source="local_pdf",
                content_hash=str(i),
                created_at=datetime(2026, 10, 17, 2),
                selection_relevance=9,
                published_date=datetime(2024, 2, 3) if i == 1 else None,
                publication_date_info_json=json.dumps(
                    {
                        "precision": "month",
                        "year": 2024,
                        "month": 2,
                        "source": "labelled_publication_date",
                        "evidence": "Published: February 2024",
                    }
                )
                if i == 2
                else None,
            )
            for i in (1, 2, 3)
        ]
        session.add_all(rows)
        session.commit()
    with (
        patch.object(daily_push, "init_db"),
        patch.object(daily_push, "get_session_factory", return_value=factory),
        patch.object(daily_push, "screening_is_current", return_value=True),
    ):
        papers, _, _ = daily_push.get_todays_papers(
            [1, 2, 3] if explicit else None, report_date="2026-10-17"
        )
    assert papers == []
    with factory() as session:
        assert session.query(Report).count() == 3


def test_daily_renderer_uses_stored_type_not_transport_source():
    import asyncio
    from tests.support.report_fixtures import paper, Model, response, module
    from reportagent.services.report_briefing import render_briefing

    p = paper(source="local_pdf", material_type="academic", title="Local paper")
    p["evaluation"] = asyncio.run(module().evaluate_paper(p, Model(response())))
    assert "**材料类型**：学术论文" in render_briefing([p], "fixture")
    p["material_type"] = "unknown"
    assert "**材料类型**：类型未知" in render_briefing([p], "fixture")


def test_legacy_creation_date_is_removed_but_unproven_existing_date_is_preserved(
    factory,
):
    with factory() as session:
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(incoming(published_date=datetime(2026, 10, 17)))
        row = session.get(Report, rid)
        row.publication_date_info_json = json.dumps(
            {"source": "pdf_creation_date", "precision": "day"}
        )
        session.commit()
        repo.upsert_report(incoming())
        assert row.published_date is None
        row.published_date = datetime(2024, 2, 3)
        row.publication_date_info_json = None
        session.commit()
        repo.upsert_report(incoming())
        assert row.published_date == source_date(datetime(2024, 2, 3))


@pytest.mark.parametrize("previous_version", [None, 1])
def test_old_unchanged_pdf_gets_metadata_once_without_model_calls(
    tmp_path, monkeypatch, previous_version
):
    import asyncio
    from reportagent.models.database import LocalPDFImport
    from reportagent.services import local_pdf_import as importer

    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    path = tmp_path / "library.pdf"
    path.write_bytes(b"%PDF-fixture")
    fingerprint = importer._file_hash(path)
    with factory() as session:
        row = Report(
            title="Existing",
            source="local_pdf",
            content_hash="same",
            abstract="Original abstract",
            classification_method="llm",
        )
        session.add(row)
        session.flush()
        session.add(
            LocalPDFImport(
                file_path=str(path.resolve()),
                file_sha256=fingerprint,
                report_id=row.id,
                report_content_hash="same",
                status="imported",
                metadata_version=previous_version,
            )
        )
        session.commit()
    calls = []

    def parse(*args):
        calls.append("parse")
        return incoming(
            published_date=datetime(2024, 2, 3),
            raw_metadata={
                "material_type": "broker",
                "material_type_info": {
                    "source": "document_header",
                    "evidence": "证券研究报告",
                },
                "publication_date_info": {
                    "precision": "day",
                    "source": "labelled_publication_date",
                    "year": 2024,
                    "month": 2,
                    "day": 3,
                    "evidence": "Published: 2024-02-03",
                },
            },
        ).search_result

    def forbidden(*args, **kwargs):
        pytest.fail("Metadata-only upgrade must not call or initialize the model")

    monkeypatch.setattr(importer, "get_session_factory", lambda: factory)
    monkeypatch.setattr(importer.LocalPDFSource, "_process_pdf", parse)
    monkeypatch.setattr(importer, "LLMClient", forbidden)

    def run():
        result = {
            "scanned": 0,
            "unchanged": 0,
            "added": 0,
            "updated": 0,
            "filtered": 0,
            "failed": 0,
            "errors": [],
            "report_ids": [],
        }
        asyncio.run(importer._scan(tmp_path, result))
        return result

    first = run()
    assert first["updated"] == 1
    with factory() as session:
        row = session.query(Report).one()
        assert row.material_type == "broker"
        assert row.published_date == source_date(datetime(2024, 2, 3))
        assert row.abstract == "Original abstract"
        assert row.classification_method == "llm"
    assert run()["unchanged"] == 1
    assert calls == ["parse"]
    engine.dispose()


def test_daily_refresh_removes_candidate_whose_date_became_unknown(factory):
    with factory() as session:
        session.add(
            Report(id=1, title="Unknown", source="local_pdf", content_hash="one")
        )
        session.commit()
    with patch.object(daily_push, "get_session_factory", return_value=factory):
        assert (
            daily_push._refresh_candidate_metadata([{"id": 1, "ui_url": "/library/1"}])
            == []
        )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (SourceType.ARXIV, "academic"),
        (SourceType.SSRN, "academic"),
        (SourceType.BROKER, "broker"),
        (SourceType.EASTMONEY, "broker"),
        (SourceType.BIGQUANT, "community"),
    ],
)
def test_typed_online_feeds_persist_type_at_ingest(factory, source, expected):
    with factory() as session:
        _, rid = ReportRepository(session).upsert_report(
            incoming(source=source, raw_metadata={})
        )
        assert session.get(Report, rid).material_type == expected


def test_schema_upgrade_is_idempotent_and_does_not_guess_local_type(
    tmp_path, monkeypatch
):
    from reportagent.db import engine as db

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE reports (id INTEGER PRIMARY KEY, title VARCHAR(500), source VARCHAR(50), content_hash VARCHAR(64), source_item_id VARCHAR(500), published_date DATETIME)"
        )
        conn.exec_driver_sql(
            "INSERT INTO reports VALUES (1, 'old local', 'local_pdf', 'one', NULL, '2024-02-03'), (2, 'old arxiv', 'arxiv', 'two', NULL, '2024-03-04')"
        )
        conn.exec_driver_sql(
            "CREATE TABLE local_pdf_imports (file_path VARCHAR(1000) PRIMARY KEY, file_sha256 VARCHAR(64), report_id INTEGER, status VARCHAR(20))"
        )
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    db.init_db()
    db.init_db()
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT material_type,published_date FROM reports ORDER BY id"
        ).all()
        assert rows == [("unknown", "2024-02-03"), ("academic", "2024-03-04")]
        assert "metadata_version" in {
            r[1] for r in conn.exec_driver_sql("PRAGMA table_info(local_pdf_imports)")
        }
    engine.dispose()


@pytest.mark.parametrize(
    ("info", "label"),
    [
        ({"precision": "year", "year": 2024}, "2024（仅年份）"),
        ({"precision": "month", "year": 2024, "month": 2}, "2024-02（仅年月）"),
    ],
)
def test_partial_publication_date_is_rendered_without_fabricated_day(info, label):
    from reportagent.services.report_briefing import _publication_date

    info.update(source="labelled_publication_date", evidence="Published date")
    assert label in _publication_date({"publication_date_info": info})


@pytest.mark.parametrize(
    ("new_date", "accepted"),
    [(datetime(2024, 2, 3), False), (datetime(2023, 8, 3), True)],
)
def test_refining_partial_date_requires_compatible_year_and_month(
    factory, new_date, accepted
):
    with factory() as session:
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(
            incoming(
                raw_metadata={
                    "publication_date_info": {
                        "precision": "month",
                        "year": 2023,
                        "month": 8,
                        "source": "labelled_publication_date",
                        "evidence": "August 2023",
                    }
                }
            )
        )
        repo.upsert_report(
            incoming(
                published_date=new_date,
                raw_metadata={
                    "publication_date_info": {
                        "precision": "day",
                        "year": new_date.year,
                        "month": new_date.month,
                        "day": new_date.day,
                        "source": "labelled_publication_date",
                        "evidence": str(new_date),
                    }
                },
            )
        )
        row = session.get(Report, rid)
        assert row.published_date == (source_date(new_date) if accepted else None)
        assert json.loads(row.publication_date_info_json)["year"] == 2023


def test_weekly_snapshot_preserves_material_type_for_shared_renderer(factory):
    from reportagent.services import weekly_report as weekly

    with factory() as session:
        row = Report(
            title="Local broker note",
            source="local_pdf",
            material_type="broker",
            content_hash="weekly",
            published_date=datetime(2024, 2, 3),
        )
        session.add(row)
        session.commit()
    with (
        patch.object(weekly, "init_db"),
        patch.object(weekly, "get_session_factory", return_value=factory),
    ):
        papers = weekly.get_weekly_papers(datetime(2024, 2, 1), datetime(2024, 2, 5))
    assert papers[0].get("material_type") == "broker"


def test_metadata_upgrade_does_not_overwrite_concurrently_committed_metadata(
    tmp_path, monkeypatch
):
    import asyncio
    from sqlalchemy.exc import OperationalError
    from reportagent.models.database import LocalPDFImport
    from reportagent.services import local_pdf_import as importer
    from reportagent.services.document_metadata import merge_document_metadata

    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent.db'}", connect_args={"timeout": 0}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-fixture")
    with factory() as session:
        row = Report(title="Existing", source="local_pdf", content_hash="same")
        session.add(row)
        session.flush()
        report_id = row.id
        session.add(
            LocalPDFImport(
                file_path=str(path.resolve()),
                file_sha256=importer._file_hash(path),
                report_id=report_id,
                report_content_hash="same",
                status="imported",
            )
        )
        session.commit()

    parsed = incoming(
        published_date=datetime(2024, 2, 3),
        raw_metadata={"material_type": "broker"},
    ).search_result
    concurrent_write = {"attempted": False, "committed": False}

    def merge_after_concurrent_write(row, search_result):
        # The importer has already loaded its row. A separate DB connection
        # models another writer committing before that stale row is merged.
        concurrent_write["attempted"] = True
        with factory() as other:
            current = other.get(Report, report_id)
            current.published_date = datetime(2020, 1, 1)
            current.material_type = "academic"
            try:
                other.commit()
                concurrent_write["committed"] = True
            except OperationalError as error:
                # A write reservation before the importer's reads is also safe:
                # the conflicting writer must retry after the upgrade commits.
                assert "locked" in str(error).lower()
                other.rollback()
        merge_document_metadata(row, search_result)

    monkeypatch.setattr(importer, "get_session_factory", lambda: factory)
    monkeypatch.setattr(importer.LocalPDFSource, "_process_pdf", lambda *args: parsed)
    monkeypatch.setattr(
        importer, "merge_document_metadata", merge_after_concurrent_write
    )
    result = {
        "scanned": 0,
        "unchanged": 0,
        "added": 0,
        "updated": 0,
        "filtered": 0,
        "failed": 0,
        "errors": [],
        "report_ids": [],
    }
    try:
        asyncio.run(importer._scan(tmp_path, result))
        assert result["updated"] == 1 and result["failed"] == 0
        assert concurrent_write["attempted"]
        expected = (
            (datetime(2020, 1, 1), "academic")
            if concurrent_write["committed"]
            else (datetime(2024, 2, 3), "broker")
        )
        with factory() as session:
            row = session.get(Report, report_id)
            assert (row.published_date, row.material_type) == (
                source_date(expected[0]),
                expected[1],
            )
    finally:
        engine.dispose()


def test_review_budget_prioritizes_stored_academic_type_not_transport():
    from reportagent.services.report_quality import select_candidates

    papers = [
        {
            "id": 1,
            "source": "arxiv",
            "material_type": "broker",
            "selection_relevance": 9,
        },
        {
            "id": 2,
            "source": "local_pdf",
            "material_type": "academic",
            "selection_relevance": 8,
        },
    ]
    assert [p["id"] for p in select_candidates(papers, max_reviews=1)] == [2]


@pytest.mark.parametrize(
    "prose",
    [
        "The conference paper by Chen et al. (2024) is used as a baseline.",
        "Proceedings of the 2024 conference are used as a baseline.",
        "Proceedings from the 2024 conference provide our benchmark data.",
    ],
)
def test_explicit_date_repairs_stored_citation_year_on_the_same_record(factory, prose):
    with factory() as session:
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(
            incoming(
                raw_metadata={
                    "publication_date_info": {
                        "precision": "year",
                        "year": 2024,
                        "source": "conference_edition",
                        "evidence": prose,
                        "conference_evidence": prose,
                    }
                }
            )
        )
        parsed = MetadataExtractor().extract(
            PDFContent(
                text="Portfolio metadata fixture\nPublished: 2026-10-16\nAbstract\n"
                + prose,
                page_count=1,
            )
        )
        action, same_id = repo.upsert_report(
            incoming(
                published_date=parsed["date"],
                raw_metadata={"publication_date_info": parsed["publication_date_info"]},
            )
        )
        session.expire_all()
        row = session.get(Report, rid)
        assert (action, same_id) == ("updated", rid)
        assert session.query(Report).count() == 1
        assert row.published_date == source_date(datetime(2026, 10, 16))
        assert json.loads(row.publication_date_info_json) == {
            "precision": "day",
            "year": 2026,
            "month": 10,
            "day": 16,
            "source": "labelled_publication_date",
            "evidence": "Published: 2026-10-16",
        }
        from reportagent.services.report_periods import is_daily_publication

        assert is_daily_publication(row, report_date="2026-10-17")
        saved_info = row.publication_date_info_json
        repo.upsert_report(incoming())
        session.expire_all()
        assert row.published_date == source_date(datetime(2026, 10, 16))
        assert row.publication_date_info_json == saved_info


@pytest.mark.parametrize(
    "evidence",
    [
        "KDD ’24, August 25–29, 2024, Barcelona, Spain",
        "The Thirty-Eighth AAAI Conference on Artificial Intelligence (AAAI-24)",
        "The 2024 Conference on Empirical Methods in Natural Language Processing",
        "Annual Quant Research Meeting, 2024",
    ],
)
def test_conflicting_reparse_preserves_supported_conference_edition(factory, evidence):
    old = {
        "precision": "year",
        "year": 2024,
        "source": "conference_edition",
        "evidence": evidence,
    }
    parsed = MetadataExtractor().extract(
        PDFContent(text="Paper title\nPublished: 2026-10-16", page_count=1)
    )
    with factory() as session:
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(
            incoming(raw_metadata={"publication_date_info": old})
        )
        repo.upsert_report(
            incoming(
                published_date=parsed["date"],
                raw_metadata={"publication_date_info": parsed["publication_date_info"]},
            )
        )
        session.expire_all()
        row = session.get(Report, rid)
        assert row.published_date is None
        assert json.loads(row.publication_date_info_json) == old


@pytest.mark.parametrize(
    "new",
    [
        {},
        {
            "precision": "day",
            "source": "labelled_publication_date",
            "year": 2026,
            "month": 10,
            "day": 16,
        },
        {
            "precision": "day",
            "source": "arxiv_revision",
            "year": 2026,
            "month": 10,
            "day": 16,
            "evidence": "arXiv:2410.12345v2 16 Oct 2026",
        },
    ],
)
@pytest.mark.parametrize("published_date", [None, datetime(2026, 10, 16)])
def test_unproven_reparse_cannot_replace_stored_conference_year(
    factory, new, published_date
):
    old = {
        "precision": "year",
        "year": 2024,
        "source": "conference_edition",
        "evidence": "The conference paper by Chen et al. (2024) is used as a baseline.",
    }
    with factory() as session:
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(
            incoming(raw_metadata={"publication_date_info": old})
        )
        repo.upsert_report(
            incoming(
                published_date=published_date,
                raw_metadata={"publication_date_info": new},
            )
        )
        session.expire_all()
        row = session.get(Report, rid)
        assert row.published_date is None
        assert json.loads(row.publication_date_info_json) == old


@pytest.mark.parametrize("value", ["2026", "October 2026"])
def test_explicit_partial_metadata_repairs_citation_without_inventing_a_day(
    factory, value
):
    old = {
        "precision": "year",
        "year": 2024,
        "source": "conference_edition",
        "evidence": "The conference paper by Chen et al. (2024) is used as a baseline.",
    }
    parsed = MetadataExtractor().extract(
        PDFContent(
            text="Portfolio metadata fixture",
            page_count=1,
            metadata={"publication_date": value},
        )
    )
    with factory() as session:
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(
            incoming(raw_metadata={"publication_date_info": old})
        )
        _, same_id = repo.upsert_report(
            incoming(
                published_date=parsed["date"],
                raw_metadata={"publication_date_info": parsed["publication_date_info"]},
            )
        )
        session.expire_all()
        row = session.get(Report, rid)
        assert same_id == rid
        assert row.published_date is None
        assert (
            json.loads(row.publication_date_info_json)
            == parsed["publication_date_info"]
        )
        assert json.loads(row.publication_date_info_json)["year"] == 2026
