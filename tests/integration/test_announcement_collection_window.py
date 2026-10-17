"""Collection and delivery share verified public announcements, not submission dates."""

import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from reportagent.models.database import Report
from reportagent.models.schemas import (
    UserCriteria,
    SearchResult,
    SourceType,
    ClassifiedReport,
    ClassificationResult,
)
from reportagent.services import (
    collection,
    daily_push,
    weekly_report,
    scheduled_collection,
)
from reportagent.services.report_periods import get_daily_range
from reportagent.db.repository import ReportRepository
from tests.support.collection_scoring import RelevanceModel


def test_schedule_enumerates_all_announcements_in_daily_window(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduled_collection, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        scheduled_collection,
        "get_config",
        lambda *a, **k: {
            "sources": ["arxiv"],
            "max_per_source": 15,
            "lookback_days": 3,
        },
    )
    observed = []

    async def collect(criteria, **kwargs):
        observed.append((criteria, kwargs))
        return {"status": "ok", "source_statuses": [], "storage": {"added": 0}}

    monkeypatch.setattr(collection, "collect_reports", collect)
    result = scheduled_collection.run_scheduled_collection()
    criteria, options = observed[0]
    start, end, _, _ = get_daily_range()
    assert isinstance(criteria, UserCriteria)
    assert criteria.max_results_per_source is None
    assert criteria.date_basis == "announcement" and criteria.date_to_exclusive
    assert (criteria.date_from, criteria.date_to) == (start, end)
    assert options == {"no_analysis": True, "broad": True}
    assert result["publication_window"]["end_exclusive"] is True


def test_announcements_survive_hydration_and_drive_both_reports(
    monkeypatch, memory_session_factory
):
    factory = memory_session_factory
    start, end, _, _ = get_daily_range("2026-10-17")
    result = SearchResult(
        title="Portfolio optimization research",
        authors=["A"],
        source=SourceType.ARXIV,
        arxiv_id="2610.00001v1",
        abstract="We study portfolio allocation and rolling out-of-sample financial risk.",
        published_date=datetime(2021, 1, 1, tzinfo=timezone.utc),
        announced_at=start + timedelta(hours=1),
        raw_metadata={
            "announcement_info": {
                "source": "arxiv_rss",
                "evidence": "Fri, 16 Oct 2026 01:00:00 +0000",
            }
        },
    )
    with factory() as db:
        repo = ReportRepository(db)
        _, rid = repo.upsert_report(
            ClassifiedReport(
                search_result=result, classification=ClassificationResult()
            )
        )
        repo.upsert_report(
            ClassifiedReport(
                search_result=result.model_copy(
                    update={"announced_at": None, "raw_metadata": {}}
                ),
                classification=ClassificationResult(),
            )
        )
        row = db.get(Report, rid)
        assert row.announced_at == datetime(2026, 10, 16, 1)
        assert row.published_date.year == 2021
    for module in (daily_push, weekly_report):
        monkeypatch.setattr(module, "init_db", lambda: None)
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    rows, _, _ = daily_push.get_todays_papers([rid], report_date="2026-10-17")
    assert [r["id"] for r in rows] == [rid]
    assert rows[0]["announced_at"].startswith("2026-10-16T01:00")
    assert [
        r["id"] for r in weekly_report.get_weekly_papers(start - timedelta(days=6), end)
    ] == [rid]


def test_unknown_arxiv_announcement_is_not_replaced_by_submission(
    monkeypatch, memory_session_factory
):
    factory = memory_session_factory
    start, end, _, _ = get_daily_range("2026-10-17")
    with factory() as db:
        db.add(
            Report(
                id=1,
                title="Only submission date",
                source="arxiv",
                content_hash="one",
                published_date=start + timedelta(hours=1),
            )
        )
        db.commit()
    for module in (daily_push, weekly_report):
        monkeypatch.setattr(module, "init_db", lambda: None)
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    assert daily_push.get_todays_papers([1], report_date="2026-10-17")[0] == []
    assert weekly_report.get_weekly_papers(start, end) == []


def test_collection_checks_window_before_scoring_or_downloading(
    monkeypatch, memory_session_factory
):
    factory = memory_session_factory
    start, end, _, _ = get_daily_range("2026-10-17")
    rows = [
        SearchResult(
            title=f"Portfolio risk model {i}",
            source=SourceType.ARXIV,
            arxiv_id=f"2610.{i:05d}v1",
            abstract="We study portfolio allocation and rolling out-of-sample financial risk models.",
            published_date=datetime(2021, 1, 1, tzinfo=timezone.utc),
            announced_at=stamp,
            abstract_only=True,
        )
        for i, stamp in enumerate(
            [start - timedelta(microseconds=1), end, None]
            + [start + timedelta(hours=1)] * 20,
            1,
        )
    ]
    source = SimpleNamespace(
        source_type=SourceType.ARXIV,
        is_available=lambda: True,
        download_pdfs=True,
        search=AsyncMock(return_value=rows),
        hydrate_result=AsyncMock(side_effect=lambda row, **kw: row),
    )
    monkeypatch.setattr(collection, "build_sources", lambda names: [source])
    monkeypatch.setattr(collection, "init_db", lambda: None)
    monkeypatch.setattr(collection, "get_session_factory", lambda: factory)
    events = []

    class Model(RelevanceModel):
        async def chat_json(self, messages, **kwargs):
            events.append("score")
            return await super().chat_json(messages, **kwargs)

    class Classifier:
        async def classify(self, title, abstract):
            events.append("classify")
            return ClassificationResult(method="fixture")

    monkeypatch.setattr(collection, "LLMClient", Model)
    monkeypatch.setattr(collection, "LLMClassifier", lambda _: Classifier())
    monkeypatch.setattr(collection, "is_quant_finance", lambda *args: False)
    criteria = UserCriteria(
        topics=["portfolio"],
        sources=[SourceType.ARXIV],
        max_results_per_source=None,
        date_from=start,
        date_to=end,
        date_basis="announcement",
        date_to_exclusive=True,
    )
    result = asyncio.run(collection.collect_reports(criteria, no_analysis=True))
    with factory() as db:
        assert db.query(Report).count() == 20
        assert {r.arxiv_id for r in db.query(Report)} == {r.arxiv_id for r in rows[3:]}
    assert result["storage"]["added"] == 20
    assert source.hydrate_result.await_count == 20
    assert events.index("score") < events.index("classify")
    assert result["source_statuses"][0]["screening"]["checked"] == 20
    assert (
        result["source_statuses"][0]["status"] == "partial"
    )  # unknown announcement is visible


def test_briefing_separates_public_announcement_from_original_submission():
    from reportagent.services.report_briefing import _publication_date

    text = _publication_date(
        {
            "source": "arxiv",
            "announced_at": "2026-10-16T04:00:00+00:00",
            "published_date": "2021-06-25T17:00:00+00:00",
        }
    )
    assert "来源公告时间：2026-10-16 12:00（北京时间）" in text
    assert "原始提交日期（UTC）：2021-06-25" in text


def test_later_cross_announcement_updates_window_without_changing_original_date(
    memory_session_factory,
):
    start, _, _, _ = get_daily_range("2026-10-17")
    sr = SearchResult(
        title="Portfolio method",
        source=SourceType.ARXIV,
        arxiv_id="2610.00001v1",
        published_date=datetime(2021, 1, 1),
        announced_at=start - timedelta(days=1),
        raw_metadata={
            "announcement_info": {"source": "arxiv_rss", "announce_type": "new"}
        },
    )
    with memory_session_factory() as db:
        repo = ReportRepository(db)
        _, rid = repo.upsert_report(
            ClassifiedReport(search_result=sr, classification=ClassificationResult())
        )
        newer = sr.model_copy(
            update={
                "announced_at": start + timedelta(hours=1),
                "raw_metadata": {
                    "announcement_info": {
                        "source": "arxiv_rss",
                        "announce_type": "cross",
                    }
                },
            }
        )
        repo.upsert_report(
            ClassifiedReport(search_result=newer, classification=ClassificationResult())
        )
        row = db.get(Report, rid)
        assert row.announced_at == datetime(2026, 10, 16, 1)
        assert row.published_date == datetime(2021, 1, 1)
        repo.upsert_report(
            ClassifiedReport(search_result=sr, classification=ClassificationResult())
        )
        assert row.announced_at == datetime(2026, 10, 16, 1)
