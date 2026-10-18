"""Sources, storage and both reports must agree on source calendar dates."""

from reportagent.sources.date_window import source_date


from datetime import date, datetime
import json

from sqlalchemy import create_engine, inspect

from reportagent.models.database import Report
from reportagent.models.schemas import SearchResult, SourceType, UserCriteria
from reportagent.services.report_periods import (
    get_daily_range,
    get_weekly_range,
    is_daily_publication,
)
from reportagent.sources.arxiv_catalog import requested_days
from reportagent.sources.eastmoney_source import EastMoneySource


def test_yesterdays_date_at_midnight_is_not_lost_by_an_eight_oclock_cutoff():
    item = EastMoneySource()._parse_item(
        {
            "title": "Portfolio research",
            "infoCode": "one",
            "publishDate": "2026-10-16 00:00:00.000",
        },
        0,
    )
    assert item.published_date == source_date(date(2026, 10, 16))
    assert is_daily_publication(item, report_date="2026-10-17")
    assert not is_daily_publication(item, report_date="2026-10-18")


def test_daily_dates_are_inclusive_and_weekly_has_seven_calendar_dates():
    start, end, *_ = get_daily_range("2026-10-19")
    assert (start, end) == (date(2026, 10, 18), date(2026, 10, 19))
    start, end, *_ = get_weekly_range(
        now=datetime.fromisoformat("2026-10-19T12:00:00+08:00")
    )
    assert (start, end) == (date(2026, 10, 13), date(2026, 10, 19))


def test_monday_batch_is_requested_on_monday_without_estimating_release_time():
    start, end, *_ = get_daily_range("2026-10-19")
    criteria = UserCriteria(
        topics=["q-fin"], date_from=start, date_to=end, date_basis="announcement"
    )
    assert date(2026, 10, 19) in requested_days(criteria)


def test_only_one_business_date_field_is_exposed():
    assert "announced_at" not in SearchResult.model_fields
    assert "announced_at" not in Report.__table__.columns
    item = SearchResult(
        title="Paper", source=SourceType.ARXIV, published_date="2026-10-16"
    )
    assert item.model_dump(mode="json")["published_date"] == "2026-10-16"


def test_legacy_migration_uses_raw_source_dates_and_removes_the_second_field():
    from reportagent.db.date_migration import migrate_source_dates

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE reports (id INTEGER PRIMARY KEY, source TEXT, published_date DATETIME, publication_date_info_json TEXT, announced_at DATETIME, announcement_info_json TEXT)"
        )
        rows = [
            (
                1,
                "arxiv",
                "2021-01-01",
                "2026-10-16 00:00:00",
                {"source": "arxiv_dated_listing", "listing_date": "2026-10-16"},
            ),
            (
                2,
                "eastmoney",
                "2026-10-17",
                "2026-10-16 16:00:00",
                {"source": "publishDate", "evidence": "2026-10-17 00:00:00.000"},
            ),
            (
                3,
                "bigquant",
                "2020-01-01",
                "2026-10-15 08:58:05",
                {
                    "source": "bigquant_api:publishedAt",
                    "evidence": "2026-10-15T08:58:05.268Z",
                },
            ),
        ]
        for rid, source, published, announced, info in rows:
            conn.exec_driver_sql(
                "INSERT INTO reports VALUES (?,?,?,NULL,?,?)",
                (rid, source, published, announced, json.dumps(info)),
            )
        migrate_source_dates(conn)
        migrate_source_dates(conn)
        assert conn.exec_driver_sql(
            "SELECT published_date FROM reports ORDER BY id"
        ).scalars().all() == ["2026-10-16", "2026-10-17", "2026-10-15"]
        assert "announced_at" not in {
            col["name"] for col in inspect(conn).get_columns("reports")
        }
        assert "announcement_info_json" not in {
            col["name"] for col in inspect(conn).get_columns("reports")
        }
