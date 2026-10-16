import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.parametrize("collect,report,expected", [
    (True, True, {"daily_collection", "feishu_daily_report"}),
    (True, False, {"daily_collection"}),
    (False, True, {"feishu_daily_report"}),
    (False, False, set()),
])
def test_collection_and_reporting_are_independent_daily_jobs(monkeypatch, collect, report, expected):
    from reportagent.services import scheduler
    configs = {"daily_collection": {"enabled": collect, "timezone": "Asia/Shanghai", "schedule_cron": "08:00"},
               "daily_report": {"enabled": report, "timezone": "Asia/Shanghai", "schedule_cron": "09:00"}}
    monkeypatch.setattr(scheduler, "get_config", lambda key, **kwargs: configs[key])
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "_run_arxiv_collection_sync", lambda: None, raising=False)

    async def run():
        scheduler.start_scheduler()
        try:
            jobs = {job.id: job for job in scheduler._scheduler.get_jobs()} if scheduler._scheduler else {}
            assert set(jobs) == expected
            # A Friday evening must schedule Saturday, not skip to Monday.
            friday = datetime(2026, 10, 16, 23, tzinfo=ZoneInfo("Asia/Shanghai"))
            if collect:
                assert jobs["daily_collection"].trigger.get_next_fire_time(None, friday).isoformat() == "2026-10-17T08:00:00+08:00"
            if report:
                assert jobs["feishu_daily_report"].trigger.get_next_fire_time(None, friday).isoformat() == "2026-10-17T09:00:00+08:00"
        finally:
            scheduler.stop_scheduler()
    asyncio.run(run())
