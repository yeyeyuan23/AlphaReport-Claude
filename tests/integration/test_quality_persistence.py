import json
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Base, Report
from reportagent.services.report_views import report_to_dict
from reportagent.models.schemas import (
    ClassifiedReport,
    ClassificationResult,
    SearchResult,
    SourceType,
)


def test_selection_keeps_direction_and_relevance_and_rescores_changed_rubric():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        row = Report(
            title="Portfolio",
            source="arxiv",
            content_hash="one",
            published_date=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        repo = ReportRepository(session)
        repo.update_selection(
            row.id,
            8,
            "Matches risk",
            True,
            direction="portfolio_optimization",
            relevance=8,
            provenance_json=json.dumps({"fingerprint": "v1"}),
        )
        assert (
            session.get(Report, row.id).selection_direction == "portfolio_optimization"
        )
        assert repo.get_unjudged_reports(scoring_fingerprint="v1") == []
        assert [r.id for r in repo.get_unjudged_reports(scoring_fingerprint="v2")] == [
            row.id
        ]
        assert report_to_dict(row)["evaluation"] is None
    engine.dispose()


def test_database_migration_is_additive_and_idempotent():
    from reportagent.db import engine as db

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE reports (id INTEGER PRIMARY KEY, title TEXT)"
        )
        conn.exec_driver_sql("INSERT INTO reports VALUES (1, 'Keep this paper')")
        conn.exec_driver_sql(
            "CREATE TABLE local_pdf_imports (file_path TEXT PRIMARY KEY, file_sha256 TEXT, report_id INTEGER, status TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO local_pdf_imports VALUES ('/paper.pdf', 'old-file-hash', 1, 'imported')"
        )
    with patch.object(db, "get_engine", return_value=engine):
        db.init_db()
        db.init_db()
    with engine.connect() as conn:
        fields = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(reports)")}
        assert {
            "evaluation_json",
            "selection_direction",
            "selection_relevance",
            "selection_provenance_json",
        } <= fields
        assert (
            conn.exec_driver_sql("SELECT title FROM reports").scalar()
            == "Keep this paper"
        )
        import_fields = {
            r[1] for r in conn.exec_driver_sql("PRAGMA table_info(local_pdf_imports)")
        }
        assert "report_content_hash" in import_fields
        assert (
            conn.exec_driver_sql("SELECT file_path FROM local_pdf_imports").scalar()
            == "/paper.pdf"
        )
    engine.dispose()


def test_concurrent_service_starts_initialize_one_consistent_database(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from reportagent.db import engine as db

    engine = create_engine(
        f"sqlite:///{tmp_path / 'library.db'}",
        connect_args={"check_same_thread": False},
    )
    ready = Barrier(4)

    def initialize(_):
        ready.wait()
        db.init_db()

    try:
        with (
            patch.object(db, "get_engine", return_value=engine),
            ThreadPoolExecutor(max_workers=4) as pool,
        ):
            list(pool.map(initialize, range(4)))
        with engine.connect() as conn:
            assert (
                conn.exec_driver_sql("SELECT COUNT(*) FROM local_pdf_imports").scalar()
                == 0
            )
            fields = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(reports)")}
            assert "publication_date_info_json" in fields
    finally:
        engine.dispose()


def test_new_revision_invalidates_previous_analysis_and_screening():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        repo = ReportRepository(session)

        def revision(number):
            return ClassifiedReport(
                search_result=SearchResult(
                    title="Same paper",
                    authors=["A"],
                    source=SourceType.ARXIV,
                    arxiv_id=f"2005.13665v{number}",
                    full_text=f"Version {number}",
                    abstract=f"Abstract {number}",
                    source_url=f"https://arxiv.org/abs/2005.13665v{number}",
                    abstract_only=False,
                ),
                classification=ClassificationResult(),
            )

        _, rid = repo.upsert_report(revision(1))
        row = session.get(Report, rid)
        row.analysis_json = row.evaluation_json = row.selection_provenance_json = (
            '{"old":true}'
        )
        row.is_selected = True
        session.commit()
        _, updated_id = repo.upsert_report(revision(2))
        row = session.get(Report, updated_id)
        assert row.arxiv_id == "2005.13665v2"
        assert row.full_text == "Version 2"
        assert (
            row.analysis_json
            is row.evaluation_json
            is row.selection_provenance_json
            is None
        )
        assert row.is_selected is False
    engine.dispose()


def test_changed_title_revision_keeps_identity_and_cannot_be_downgraded():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        repo = ReportRepository(session)

        def revision(number):
            return ClassifiedReport(
                search_result=SearchResult(
                    title=f"Title version {number}",
                    authors=["A"],
                    source=SourceType.ARXIV,
                    arxiv_id=f"2005.13665v{number}",
                    full_text=f"Version {number}",
                    abstract=f"Abstract {number}",
                    source_url=f"https://arxiv.org/abs/2005.13665v{number}",
                    abstract_only=False,
                ),
                classification=ClassificationResult(),
            )

        _, original = repo.upsert_report(revision(1))
        _, updated = repo.upsert_report(revision(2))
        assert original == updated
        assert session.get(Report, original).title == "Title version 2"
        _, same = repo.upsert_report(revision(1))
        assert same == original
        assert session.get(Report, same).arxiv_id == "2005.13665v2"
        assert session.query(Report).count() == 1
    engine.dispose()


def test_legacy_quote_gate_is_shown_as_outdated_assessment_not_active_screening():
    from reportagent.services.report_views import read_evaluation

    legacy = {
        "schema_version": 3,
        "tier": "pending",
        "label": "待核验",
        "status": "needs_verification",
        "quality": "sound",
        "validation_issues": ["引文未能定位：C005"],
        "findings": [],
    }
    shown = read_evaluation(json.dumps(legacy))
    assert shown["tier"] == "pending"
    assert "规则已更新" in " ".join(shown["validation_issues"])
    assert "引文未能定位" not in " ".join(shown["validation_issues"])
    assert legacy["validation_issues"] == ["引文未能定位：C005"]


def test_saved_completed_evaluation_exposes_readable_relevance_reason():
    from reportagent.services.report_views import read_evaluation
    from reportagent.services.report_quality import SCHEMA_VERSION

    saved = {
        "schema_version": SCHEMA_VERSION,
        "status": "reviewed",
        "tier": "focus",
        "relevance_reason": "[portfolio_optimization] [仅摘要相关性；全文质量待评审] 与组合优化有关。",
    }
    shown = read_evaluation(json.dumps(saved))
    assert shown["relevance_reason"] == "与组合优化有关。"
    assert shown["status"] == "reviewed" and shown["tier"] == "focus"
