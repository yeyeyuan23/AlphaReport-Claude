"""Daily and weekly publication windows end at the Beijing collection cutoff."""

from datetime import date, datetime, timezone

import pytest

from reportagent.models.database import Report
from reportagent.services import daily_push as daily, weekly_report as weekly


@pytest.mark.parametrize(
    "now,first,last",
    [
        (datetime(2026, 10, 17, 4, tzinfo=timezone.utc), "2026-10-11", "2026-10-17"),
        (datetime(2026, 10, 18, 16, tzinfo=timezone.utc), "2026-10-13", "2026-10-19"),
        (datetime(2027, 1, 1, tzinfo=timezone.utc), "2026-12-26", "2027-01-01"),
        (datetime(2024, 3, 1, tzinfo=timezone.utc), "2024-02-24", "2024-03-01"),
    ],
)
def test_weekly_period_contains_seven_calendar_dates(now, first, last):
    from reportagent.services.report_periods import get_weekly_range

    start, end, actual_first, actual_last = get_weekly_range(now=now)
    assert (actual_first, actual_last) == (first, last)
    assert start == date.fromisoformat(first)
    assert end == date.fromisoformat(last)


def test_weekly_backfill_offsets_the_rolling_window_by_seven_days():
    from reportagent.services.report_periods import get_weekly_range

    _, _, first, last = get_weekly_range(
        1, now=datetime(2026, 10, 17, 4, tzinfo=timezone.utc)
    )
    assert (first, last) == ("2026-10-04", "2026-10-10")
    with pytest.raises(ValueError):
        get_weekly_range(-1)


def test_daily_uses_publication_day_regardless_of_ingestion(
    monkeypatch, memory_session_factory
):
    factory = memory_session_factory
    monkeypatch.setattr(daily, "init_db", lambda: None)
    monkeypatch.setattr(daily, "get_session_factory", lambda: factory)
    monkeypatch.setattr(weekly, "init_db", lambda: None)
    monkeypatch.setattr(weekly, "get_session_factory", lambda: factory)
    monkeypatch.setattr(daily, "screening_is_current", lambda row: True)
    with factory() as db:
        for rid, created, published in [
            (1, "2026-10-16T16:00:00", "2026-10-15T00:00:00"),
            (2, "2026-10-17T15:59:59.999999", None),
            (3, "2026-10-16T15:59:59.999999", "2026-10-16T12:00:00"),
            (4, "2026-10-17T16:00:00", "2026-10-16T12:00:00"),
        ]:
            db.add(
                Report(
                    id=rid,
                    title=f"Paper {rid}",
                    source="arxiv",
                    content_hash=str(rid),
                    created_at=datetime.fromisoformat(created),
                    published_date=datetime.fromisoformat(published)
                    if published
                    else None,
                    selection_relevance=8,
                )
            )
        db.commit()
    rows, first, last = daily.get_todays_papers(report_date="2026-10-17")
    assert {r["id"] for r in rows} == {3, 4}
    assert (first, last) == ("2026-10-17", "2026-10-17")
    # A publication-based weekly report must not substitute ingestion dates.
    from reportagent.services.report_periods import get_weekly_range

    start, end, _, _ = get_weekly_range(
        now=datetime(2026, 10, 17, 4, tzinfo=timezone.utc)
    )
    assert {r["id"] for r in weekly.get_weekly_papers(start, end)} == {1, 3, 4}


def test_daily_empty_report_keeps_the_requested_report_date(
    monkeypatch, memory_session_factory
):
    monkeypatch.setattr(daily, "init_db", lambda: None)
    monkeypatch.setattr(daily, "get_session_factory", lambda: memory_session_factory)
    assert daily.get_todays_papers(report_date="2026-10-17") == (
        [],
        "2026-10-17",
        "2026-10-17",
    )


def test_manual_created_date_scoring_respects_beijing_boundaries(
    monkeypatch, memory_session_factory
):
    import asyncio
    from types import SimpleNamespace
    from reportagent.services import paper_scoring as scoring

    factory = memory_session_factory
    with factory() as db:
        for rid, created in [
            (1, "2026-10-16T16:00:00"),
            (2, "2026-10-16T15:59:59"),
            (3, "2026-10-17T16:00:00"),
        ]:
            db.add(
                Report(
                    id=rid,
                    title=f"Paper {rid}",
                    content_hash=str(rid),
                    source="arxiv",
                    abstract="We study portfolio allocation with transaction costs and rolling out-of-sample returns.",
                    created_at=datetime.fromisoformat(created),
                )
            )
        db.commit()
    monkeypatch.setattr(scoring, "init_db", lambda: None)
    monkeypatch.setattr(scoring, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        scoring,
        "LLMClient",
        lambda: SimpleNamespace(provider="offline", model="fixture"),
    )
    observed = []

    async def assess(model, papers, **kwargs):
        observed.extend(p.id for p in papers)
        return [], []

    monkeypatch.setattr(scoring, "score_papers_batch", assess)
    asyncio.run(
        scoring.score_reports(
            scoring.ScoringOptions(
                created_date="2026-10-17", max_papers=0, dry_run=True
            )
        )
    )
    assert observed == [1]


@pytest.mark.parametrize("explicit", [False, True])
def test_daily_precise_publication_boundaries(
    monkeypatch, memory_session_factory, explicit
):
    import json

    factory = memory_session_factory
    monkeypatch.setattr(daily, "init_db", lambda: None)
    monkeypatch.setattr(daily, "get_session_factory", lambda: factory)
    monkeypatch.setattr(daily, "screening_is_current", lambda row: True)
    with factory() as db:
        for rid, published, info in [
            (1, "2026-10-16T00:00:00", None),
            (2, "2026-10-16T23:59:59.999999", None),
            (3, "2026-10-15T23:59:59.999999", None),
            (4, "2026-10-17T00:00:00", None),
            (5, None, {"precision": "year", "year": 2021}),
            (6, None, {"precision": "month", "year": 2026, "month": 10}),
            (
                7,
                "2026-10-16T12:00:00",
                {"precision": "month", "year": 2026, "month": 10},
            ),
            (8, "2026-10-16T12:00:00", {"source": "pdf_creation_date"}),
        ]:
            db.add(
                Report(
                    id=rid,
                    title=f"Paper {rid}",
                    source="local_pdf",
                    content_hash=str(rid),
                    created_at=datetime(2020, 1, 1),
                    published_date=datetime.fromisoformat(published)
                    if published and info is None
                    else None,
                    publication_date_info_json=json.dumps(info) if info else None,
                    selection_relevance=8,
                )
            )
        db.commit()
    rows, _, _ = daily.get_todays_papers(
        list(range(1, 9)) if explicit else None, report_date="2026-10-17"
    )
    assert {r["id"] for r in rows} == {1, 2, 4}
    with factory() as db:
        assert db.query(Report).count() == 8


def test_relevance_refresh_uses_exact_publication_day(
    monkeypatch, memory_session_factory
):
    import asyncio
    import json
    from types import SimpleNamespace
    from reportagent.services import paper_scoring as scoring

    factory = memory_session_factory
    with factory() as db:
        for rid, published, precision in [
            (1, "2026-10-16T00:00:00", None),
            (2, "2026-10-16T23:59:59.999999", None),
            (3, "2026-10-15T23:59:59.999999", None),
            (4, "2026-10-17T00:00:00", None),
            (5, "2026-10-16T12:00:00", "year"),
            (6, None, "month"),
        ]:
            db.add(
                Report(
                    id=rid,
                    title=f"Paper {rid}",
                    content_hash=str(rid),
                    source="arxiv",
                    abstract="We study portfolio allocation with transaction costs and rolling out-of-sample returns.",
                    created_at=datetime(2020, 1, 1),
                    published_date=datetime.fromisoformat(published)
                    if published and precision is None
                    else None,
                    publication_date_info_json=json.dumps({"precision": precision})
                    if precision
                    else None,
                )
            )
        db.commit()
    monkeypatch.setattr(scoring, "init_db", lambda: None)
    monkeypatch.setattr(scoring, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        scoring,
        "LLMClient",
        lambda: SimpleNamespace(provider="offline", model="fixture"),
    )
    observed = []

    async def assess(model, papers, **kwargs):
        observed.extend(p.id for p in papers)
        return [], []

    monkeypatch.setattr(scoring, "score_papers_batch", assess)
    asyncio.run(
        scoring.score_reports(
            scoring.ScoringOptions(
                published_date="2026-10-17", max_papers=0, dry_run=True
            )
        )
    )
    assert set(observed) == {1, 2, 4}


@pytest.mark.parametrize("published", [None, datetime(2021, 1, 1)])
def test_daily_refresh_drops_changed_publication_date(
    monkeypatch, memory_session_factory, published
):
    factory = memory_session_factory
    with factory() as db:
        db.add(
            Report(
                id=1,
                title="Changed",
                source="local_pdf",
                content_hash="one",
                published_date=published,
            )
        )
        db.commit()
    monkeypatch.setattr(daily, "get_session_factory", lambda: factory)
    assert (
        daily._refresh_candidate_metadata(
            [{"id": 1, "ui_url": "/library/1"}], report_date="2026-10-17"
        )
        == []
    )


@pytest.mark.parametrize("report_date", ["2026-10-17", "2027-01-01", "2024-03-01"])
def test_daily_window_includes_yesterday_and_report_date(report_date):
    from datetime import timedelta
    from reportagent.services.report_periods import get_daily_range

    start, end, label, _ = get_daily_range(report_date)
    assert end == date.fromisoformat(report_date)
    assert end - start == timedelta(days=1)
    assert label == report_date


def test_weekly_publication_query_excludes_cutoff_and_partial_dates(
    monkeypatch, memory_session_factory
):
    import json
    from reportagent.services.report_periods import get_weekly_range
    from datetime import timedelta

    start, end, _, _ = get_weekly_range(
        now=datetime(2026, 10, 17, 4, tzinfo=timezone.utc)
    )
    factory = memory_session_factory
    monkeypatch.setattr(weekly, "init_db", lambda: None)
    monkeypatch.setattr(weekly, "get_session_factory", lambda: factory)
    with factory() as db:
        for rid, published, info in [
            (1, start, None),
            (2, end - timedelta(days=1), None),
            (3, start - timedelta(days=1), None),
            (4, end, None),
            (5, start, {"precision": "month"}),
            (6, start, {"source": "pdf_creation_date"}),
        ]:
            db.add(
                Report(
                    id=rid,
                    title=f"Paper {rid}",
                    source="arxiv",
                    content_hash=str(rid),
                    published_date=published if info is None else None,
                    publication_date_info_json=json.dumps(info) if info else None,
                )
            )
        db.commit()
    assert {p["id"] for p in weekly.get_weekly_papers(start, end)} == {1, 2, 4}


def test_weekly_body_displays_the_actual_date_range():
    from reportagent.services.report_periods import (
        get_weekly_range,
        format_publication_window,
    )

    start, end, first, last = get_weekly_range(
        now=datetime(2026, 10, 17, 4, tzinfo=timezone.utc)
    )
    text = weekly.generate_weekly_report(
        [], first, last, publication_window=format_publication_window(start, end)
    )
    assert "来源日期范围：2026-10-11 ~ 2026-10-17" in text
    assert "来源日期，含起止日期" in text
