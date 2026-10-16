import json
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Base, Report
from reportagent.services.report_views import report_to_dict
from reportagent.models.schemas import ClassifiedReport, ClassificationResult, SearchResult, SourceType


def test_selection_keeps_direction_and_relevance_and_rescores_changed_rubric():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        row = Report(title="Portfolio", source="arxiv", content_hash="one", published_date=datetime.now(timezone.utc))
        session.add(row)
        session.commit()
        repo = ReportRepository(session)
        repo.update_selection(row.id, 8, "Matches risk", True, direction="portfolio_optimization", relevance=8,
                              provenance_json=json.dumps({"fingerprint": "v1"}))
        assert session.get(Report, row.id).selection_direction == "portfolio_optimization"
        assert repo.get_unjudged_reports(scoring_fingerprint="v1") == []
        assert [r.id for r in repo.get_unjudged_reports(scoring_fingerprint="v2")] == [row.id]
        assert report_to_dict(row)["evaluation"] is None
    engine.dispose()


def test_database_migration_is_additive_and_idempotent():
    from reportagent.db import engine as db
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE reports (id INTEGER PRIMARY KEY, title TEXT)")
        conn.exec_driver_sql("INSERT INTO reports VALUES (1, 'Keep this paper')")
    with patch.object(db, "get_engine", return_value=engine):
        db.init_db()
        db.init_db()
    with engine.connect() as conn:
        fields = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(reports)")}
        assert {"evaluation_json", "selection_direction", "selection_relevance", "selection_provenance_json"} <= fields
        assert conn.exec_driver_sql("SELECT title FROM reports").scalar() == "Keep this paper"
    engine.dispose()


def test_new_revision_invalidates_previous_analysis_and_screening():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        repo = ReportRepository(session)
        def revision(number):
            return ClassifiedReport(search_result=SearchResult(title="Same paper", authors=["A"], source=SourceType.ARXIV,
                arxiv_id=f"2005.13665v{number}", full_text=f"Version {number}", abstract=f"Abstract {number}",
                source_url=f"https://arxiv.org/abs/2005.13665v{number}", abstract_only=False), classification=ClassificationResult())
        _, rid = repo.upsert_report(revision(1))
        row = session.get(Report, rid)
        row.analysis_json = row.evaluation_json = row.selection_provenance_json = '{"old":true}'
        row.is_selected = True
        session.commit()
        _, updated_id = repo.upsert_report(revision(2))
        row = session.get(Report, updated_id)
        assert row.arxiv_id == "2005.13665v2"
        assert row.full_text == "Version 2"
        assert row.analysis_json is row.evaluation_json is row.selection_provenance_json is None
        assert row.is_selected is False
    engine.dispose()


def test_changed_title_revision_keeps_identity_and_cannot_be_downgraded():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        repo = ReportRepository(session)
        def revision(number):
            return ClassifiedReport(search_result=SearchResult(title=f"Title version {number}", authors=["A"], source=SourceType.ARXIV,
                arxiv_id=f"2005.13665v{number}", full_text=f"Version {number}", abstract=f"Abstract {number}",
                source_url=f"https://arxiv.org/abs/2005.13665v{number}", abstract_only=False), classification=ClassificationResult())
        _, original = repo.upsert_report(revision(1))
        _, updated = repo.upsert_report(revision(2))
        assert original == updated
        assert session.get(Report, original).title == "Title version 2"
        _, same = repo.upsert_report(revision(1))
        assert same == original
        assert session.get(Report, same).arxiv_id == "2005.13665v2"
        assert session.query(Report).count() == 1
    engine.dispose()
