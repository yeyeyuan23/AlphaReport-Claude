"""Report queues progress with offline SQLite, document and model adapters."""

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.db import engine as database
from reportagent.models.database import Base, Report
from reportagent.services import (
    daily_push as daily,
    weekly_report as weekly,
    one_report,
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
def queue_library(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for service in (daily, weekly, database):
        monkeypatch.setattr(service, "get_session_factory", lambda: factory)
    for service in (daily, weekly):
        monkeypatch.setattr(service, "init_db", lambda: None)
    monkeypatch.setattr(daily, "PROJECT_ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/push_directions.yaml").write_text("push_settings: {}")
    monkeypatch.setattr(
        daily.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0),
    )
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
    now = datetime.now(timezone.utc)
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
            if rid <= 12:
                values.update(full_text="", has_full_text=False)
            row = Report(**values)
            if rid == 13:
                cached = asyncio.run(quality.evaluate_paper(values, QueueModel()))
                row.evaluation_json = json.dumps(cached)
            session.add(row)
        session.commit()
    yield factory, model, fetch
    engine.dispose()


@pytest.mark.parametrize("flow", ["weekly", "daily"])
def test_missing_material_and_current_caches_do_not_starve_two_review_runs(
    queue_library, flow
):
    factory, model, _ = queue_library
    for expected_last_id in (25, 37):
        before = len(model.requests)
        if flow == "weekly":
            papers = weekly.get_weekly_papers(
                datetime(2020, 1, 1), datetime(2030, 1, 1)
            )
            asyncio.run(weekly.prepare_weekly_evidence(papers))
        else:
            daily.run_daily_push(daily.DailyPushOptions(dry_run=True))
        assert len(model.requests) - before == 12
        with factory() as session:
            for rid in range(1, 13):
                evaluation = json.loads(session.get(Report, rid).evaluation_json)
                assert evaluation["tier"] == "pending"
            for rid in range(13, expected_last_id + 1):
                evaluation = json.loads(session.get(Report, rid).evaluation_json)
                assert evaluation["status"] == "reviewed", rid
            assert session.get(Report, expected_last_id + 1).evaluation_json is None


def test_weekly_fallback_recipient_uses_daily_default_open_id(
    queue_library, monkeypatch
):
    monkeypatch.delenv("FEISHU_WEEKLY_PUSH_IDS", raising=False)
    monkeypatch.delenv("FEISHU_WEEKLY_PUSH_TYPE", raising=False)
    monkeypatch.delenv("FEISHU_DAILY_PUSH_TYPE", raising=False)
    monkeypatch.setenv("FEISHU_DAILY_PUSH_IDS", "daily-user")
    monkeypatch.setattr(
        weekly,
        "get_previous_week_range",
        lambda *args: (
            datetime(2020, 1, 1),
            datetime(2030, 1, 1),
            "2020-01-01",
            "2030-01-01",
        ),
    )
    recipients = []
    monkeypatch.setattr(
        weekly,
        "send_feishu_card",
        lambda rid, text, papers, start, end, kind: recipients.append((rid, kind))
        or True,
    )
    weekly.run_weekly_report(weekly.WeeklyReportOptions(no_llm=True))
    assert recipients == [("daily-user", "open_id")]


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
        assert fetch.await_count == 1
    else:
        assert result[13]["status"] == "reviewed"
        assert 14 not in result
    assert len(model.requests) == 1
