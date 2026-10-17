"""Daily updates follow ingestion; weekly coverage rolls with publication dates."""

from datetime import datetime, timezone

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
def test_weekly_period_is_seven_beijing_dates_including_today(now, first, last):
    from reportagent.services.report_periods import get_weekly_range

    start, end, actual_first, actual_last = get_weekly_range(now=now)
    assert (actual_first, actual_last) == (first, last)
    assert start == datetime.fromisoformat(f"{first}T00:00:00+08:00")
    assert end == datetime.fromisoformat(f"{last}T23:59:59.999999+08:00")


def test_weekly_backfill_offsets_the_rolling_window_by_seven_days():
    from reportagent.services.report_periods import get_weekly_range

    _, _, first, last = get_weekly_range(
        1, now=datetime(2026, 10, 17, 4, tzinfo=timezone.utc)
    )
    assert (first, last) == ("2026-10-04", "2026-10-10")
    with pytest.raises(ValueError):
        get_weekly_range(-1)


def test_daily_uses_beijing_ingestion_day_but_excludes_unknown_publication(
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
            (3, "2026-10-16T15:59:59.999999", "2026-10-17T00:00:00"),
            (4, "2026-10-17T16:00:00", "2026-10-17T00:00:00"),
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
    assert {r["id"] for r in rows} == {1}
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


def test_relevance_refresh_matches_daily_ingestion_boundaries(
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
