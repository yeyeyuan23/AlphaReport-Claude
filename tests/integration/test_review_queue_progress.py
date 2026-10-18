"""Report queues progress with offline SQLite, document and model adapters."""

import asyncio
import json
import subprocess
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from reportagent.db import engine as database
from reportagent.services.report_periods import get_daily_range
from reportagent.models.database import Report
from reportagent.services import (
    daily_push as daily,
    weekly_report as weekly,
    one_report,
    collection_reviews,
)
from reportagent.services import report_quality as quality
from tests.support.report_fixtures import Model, paper, response


class QueueModel(Model):
    def __init__(self):
        super().__init__(response())
        self.model = quality.get_llm_model()
        self._client = self

    async def close(self):
        pass


@pytest.fixture
def queue_library(tmp_path, monkeypatch, memory_session_factory):
    factory = memory_session_factory
    for service in (daily, weekly, database, collection_reviews):
        monkeypatch.setattr(service, "get_session_factory", lambda: factory)
    for service in (daily, weekly):
        monkeypatch.setattr(service, "init_db", lambda: None)
    monkeypatch.setattr(daily, "PROJECT_ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/push_directions.yaml").write_text("push_settings: {}")
    monkeypatch.setattr(
        daily,
        "send_feishu_card",
        lambda *args, **kwargs: pytest.fail("No real delivery"),
    )
    model = QueueModel()
    monkeypatch.setattr(
        __import__("reportagent.llm.client", fromlist=["LLMClient"]),
        "LLMClient",
        lambda **kwargs: model,
    )
    fetch = AsyncMock(side_effect=RuntimeError("offline PDF unavailable"))
    monkeypatch.setattr(one_report, "collect_paper", fetch)
    now = get_daily_range()[0] + timedelta(hours=1)
    with factory() as session:
        for rid in range(1, 39):
            values = paper(
                id=rid,
                title=f"Portfolio study {rid}",
                content_hash=str(rid),
                arxiv_id=f"2610.{rid:05d}v1",
                published_date=now,
                selection_direction="portfolio_optimization",
            )
            if rid < 12:
                values.update(full_text="", has_full_text=False)
            elif rid == 12:
                values.update(full_text="x" * 350000)
            row = Report(**values)
            if rid == 13:
                cached = asyncio.run(quality.evaluate_paper(values, QueueModel()))
                row.evaluation_json = json.dumps(cached)
            session.add(row)
        session.commit()
    yield factory, model, fetch


def test_missing_material_and_current_caches_do_not_starve_collection_reviews(
    queue_library,
):
    factory, model, fetch = queue_library
    for expected_last_id in (24, 36):
        before = len(model.requests)
        asyncio.run(collection_reviews.prepare_collection_reviews(max_reviews=12))
        assert len(model.requests) - before == 12
        with factory() as session:
            for rid in range(1, 12):
                assert session.get(Report, rid).evaluation_json is None
            for rid in range(12, expected_last_id + 1):
                assert (
                    json.loads(session.get(Report, rid).evaluation_json)["status"]
                    == "reviewed"
                )
            assert session.get(Report, expected_last_id + 1).evaluation_json is None
    fetch.assert_not_awaited()


def test_generic_review_entrypoint_keeps_full_requested_library_contract(queue_library):
    _, model, _ = queue_library
    result = asyncio.run(quality.review_report_ids(list(range(14, 39))))
    assert len(model.requests) == 25
    assert set(result) == set(range(14, 39))


@pytest.mark.parametrize("invalid_field", ["has_full_text", "text_complete"])
def test_cache_without_current_complete_material_is_not_reused(
    queue_library, invalid_field
):
    factory, model, fetch = queue_library
    with factory() as session:
        row = session.get(Report, 13)
        if invalid_field == "has_full_text":
            row.has_full_text = False
        else:
            cached = json.loads(row.evaluation_json)
            cached["text_complete"] = False
            row.evaluation_json = json.dumps(cached)
        session.commit()
    result = asyncio.run(quality.review_report_ids([13, 14], max_reviews=1))
    if invalid_field == "has_full_text":
        assert result[13]["tier"] == "pending"
        assert result[14]["status"] == "reviewed"
        fetch.assert_not_awaited()
    else:
        assert result[13]["status"] == "reviewed"
        assert 14 not in result
    assert len(model.requests) == 1


@pytest.mark.parametrize("budget", [None, 2])
def test_collection_review_budget_defaults_to_all_and_resumes(queue_library, budget):
    factory, model, _ = queue_library
    result = asyncio.run(
        collection_reviews.prepare_collection_reviews(max_reviews=budget)
    )
    expected = 26 if budget is None else budget
    assert len(model.requests) == expected
    assert len(result["review_ids"]) == expected
    assert len(result["completed_ids"]) == expected
    assert result["cached_ids"] == [13]
    assert len(result["deferred_ids"]) == 26 - expected
    if budget is None:
        repeated = asyncio.run(collection_reviews.prepare_collection_reviews())
        assert len(model.requests) == 26
        assert repeated["review_ids"] == repeated["completed_ids"] == []
        assert set(repeated["cached_ids"]) == set(range(12, 39))


@pytest.mark.parametrize("flow", ["weekly", "daily"])
def test_reports_never_download_or_review_and_preserve_library(
    queue_library, monkeypatch, capsys, flow
):
    factory, model, fetch = queue_library
    monkeypatch.setattr(
        weekly,
        "get_weekly_range",
        lambda *a: (
            datetime(2020, 1, 1),
            datetime(2030, 1, 1),
            "2020-01-01",
            "2030-01-01",
        ),
    )
    with factory() as db:
        before = {r.id: r.evaluation_json for r in db.query(Report)}
    if flow == "weekly":
        result = weekly.run_weekly_report(weekly.WeeklyReportOptions(dry_run=True))
    else:
        result = daily.run_daily_push(daily.DailyPushOptions(dry_run=True))
    assert not model.requests
    fetch.assert_not_awaited()
    with factory() as db:
        assert {r.id: r.evaluation_json for r in db.query(Report)} == before
    assert result["status"] == "preview"
    text = capsys.readouterr().out
    for rid in range(1, 12):
        assert f"**Portfolio study {rid}**" not in text
        assert f"[Portfolio study {rid}]" not in text


def test_provider_context_limit_is_saved_skipped_without_blocking_or_repeating_siblings(
    queue_library,
):
    from reportagent.llm.errors import ContextWindowExceeded

    factory, model, fetch = queue_library
    original = model.chat_json

    async def reject_long_request(messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        if len("".join(payload["text_chunks"].values())) > 300000:
            model.requests.append(messages)
            raise ContextWindowExceeded(model.model)
        return await original(messages, **kwargs)

    model.chat_json = reject_long_request
    result = asyncio.run(collection_reviews.prepare_collection_reviews())
    assert len(model.requests) == 26
    assert result["completed_ids"] == list(range(14, 39))
    assert set(result["skipped_material_ids"]) == set(range(1, 13))
    assert not result["failures"]
    with factory() as session:
        row = session.get(Report, 12)
        saved = json.loads(row.evaluation_json)
        assert saved["status"] == "skipped"
        assert saved["skip_reason"] == "context_length_exceeded"
        assert model.model in saved["validation_issues"][0]
        assert not quality.has_review_material(row)
    displayed = weekly.get_weekly_papers(datetime(2020, 1, 1), datetime(2030, 1, 1))
    assert displayed and 12 not in {paper["id"] for paper in displayed}
    repeated = asyncio.run(collection_reviews.prepare_collection_reviews())
    assert len(model.requests) == 26
    assert not repeated["review_ids"]
    direct = asyncio.run(quality.review_report_ids([12]))
    assert direct[12] == saved
    assert len(model.requests) == 26
    with factory() as session:
        row = session.get(Report, 12)
        row.full_text = "New complete evidence. " * 100
        session.commit()
        assert quality.has_review_material(row)
    retried = asyncio.run(collection_reviews.prepare_collection_reviews())
    assert retried["completed_ids"] == [12]
    fetch.assert_not_awaited()


def test_default_candidate_ordering_does_not_truncate_after_twelve():
    rows = [
        paper(id=rid, selection_direction="portfolio_optimization")
        for rid in range(1, 31)
    ]
    assert len(quality.select_candidates(rows)) == 30


def test_daily_query_keeps_more_than_two_hundred_current_candidates(queue_library):
    factory, _, _ = queue_library
    with factory() as db:
        for rid in range(39, 242):
            db.add(
                Report(
                    **paper(
                        id=rid,
                        title=f"Portfolio study {rid}",
                        content_hash=str(rid),
                        published_date=get_daily_range()[0] + timedelta(hours=1),
                    )
                )
            )
        db.commit()
    assert len(daily.get_todays_papers()[0]) == 241


def test_daily_reuses_screening(queue_library, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Report generation must not launch a relevance scoring subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    result = daily.run_daily_push(daily.DailyPushOptions(dry_run=True))
    assert result["stages"]["relevance"]["status"] == "not_run"
