"""Daily content depends on publication date/screening, never send markers."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.services.report_periods import get_daily_range
from reportagent.models.database import Base, Report
from reportagent.services import daily_push as daily
from reportagent.services.paper_scoring import evidence_fingerprint, scoring_fingerprint


@pytest.fixture
def legacy_library(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = get_daily_range()[0] + timedelta(hours=1)
    with factory() as session:
        for rid, date, score in [
            (1, now, 8),
            (2, now - timedelta(days=10), 8),
            (3, now, 3),
            (4, None, 8),
            (5, now, 8),
        ]:
            row = Report(
                id=rid,
                title=f"Portfolio study {rid}",
                source="arxiv",
                content_hash=str(rid),
                published_date=date,
                announced_at=date,
                selection_relevance=score,
                abstract="We study portfolio allocation and test financial return prediction.",
            )
            row.selection_provenance_json = json.dumps(
                {
                    "fingerprint": scoring_fingerprint(),
                    "status": "scored",
                    "evidence_fingerprint": evidence_fingerprint(vars(row)),
                }
            )
            if rid == 5:
                row.abstract = "Changed evidence invalidates the earlier score."
            session.add(row)
        session.commit()
    # Simulate a database written by an older installation. New code must work
    # against it without reading, updating or dropping its historical markers.
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(reports)")}
        if "pushed_at" not in columns:
            conn.exec_driver_sql("ALTER TABLE reports ADD COLUMN pushed_at TEXT")
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS report_deliveries (report_id INTEGER, receive_type TEXT, receive_id TEXT, delivered_at TEXT)"
        )
        conn.exec_driver_sql(
            "UPDATE reports SET pushed_at='2026-10-16T00:00:00Z' WHERE id=1"
        )
        conn.exec_driver_sql(
            "INSERT INTO report_deliveries VALUES (1,'chat_id','A','2026-10-16T00:00:00Z')"
        )
    monkeypatch.setattr(daily, "init_db", lambda: None)
    monkeypatch.setattr(daily, "get_session_factory", lambda: factory)
    monkeypatch.setattr(daily, "PUSH_SETTINGS", {"llm_filter_threshold": 6})
    yield factory
    engine.dispose()


def test_publication_day_and_current_relevance_select_previously_sent_papers(
    legacy_library,
):
    papers, _, _ = daily.get_todays_papers()
    assert {p["id"] for p in papers} == {1}


def test_explicit_paper_ids_are_not_excluded_by_old_send_markers(legacy_library):
    papers, _, _ = daily.get_todays_papers(report_ids=[1])
    assert [p["id"] for p in papers] == [1]


def test_explicit_ids_cannot_bypass_daily_publication_window(legacy_library):
    papers, _, _ = daily.get_todays_papers(report_ids=[2, 4])
    assert papers == []
