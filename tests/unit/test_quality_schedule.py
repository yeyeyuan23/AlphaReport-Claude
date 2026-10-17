import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.parametrize(
    "collect,report,weekly,expected",
    [
        (
            True,
            True,
            True,
            {"daily_collection", "feishu_daily_report", "feishu_weekly_report"},
        ),
        (True, True, False, {"daily_collection", "feishu_daily_report"}),
        (True, False, True, {"daily_collection", "feishu_weekly_report"}),
        (True, False, False, {"daily_collection"}),
        (False, True, True, {"feishu_daily_report", "feishu_weekly_report"}),
        (False, True, False, {"feishu_daily_report"}),
        (False, False, True, {"feishu_weekly_report"}),
        (False, False, False, set()),
    ],
)
def test_collection_and_reports_have_independent_schedule_switches(
    monkeypatch, collect, report, weekly, expected
):
    from reportagent.services import scheduler

    configs = {
        "daily_collection": {
            "enabled": collect,
            "timezone": "Asia/Shanghai",
            "schedule_cron": "08:00",
        },
        "daily_report": {
            "enabled": report,
            "timezone": "Asia/Shanghai",
            "schedule_cron": "09:00",
        },
        "weekly_report": {"enabled": weekly},
    }
    monkeypatch.setattr(scheduler, "get_config", lambda key, **kwargs: configs[key])
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(
        scheduler, "_run_arxiv_collection_sync", lambda: None, raising=False
    )

    async def run():
        scheduler.start_scheduler()
        try:
            jobs = (
                {job.id: job for job in scheduler._scheduler.get_jobs()}
                if scheduler._scheduler
                else {}
            )
            assert set(jobs) == expected
            status = scheduler.get_schedule_status()
            assert status["running"] is bool(expected)
            assert status["config_enabled"] is bool(expected)
            assert status["weekly_enabled"] is weekly
            assert bool(status["weekly_next_run"]) is weekly
            # A Friday evening must schedule Saturday, not skip to Monday.
            friday = datetime(2026, 10, 16, 23, tzinfo=ZoneInfo("Asia/Shanghai"))
            if collect:
                assert (
                    jobs["daily_collection"]
                    .trigger.get_next_fire_time(None, friday)
                    .isoformat()
                    == "2026-10-17T08:00:00+08:00"
                )
            if report:
                assert (
                    jobs["feishu_daily_report"]
                    .trigger.get_next_fire_time(None, friday)
                    .isoformat()
                    == "2026-10-17T09:00:00+08:00"
                )
            if weekly:
                assert (
                    jobs["feishu_weekly_report"]
                    .trigger.get_next_fire_time(None, friday)
                    .isoformat()
                    == "2026-10-23T09:00:00+08:00"
                )
        finally:
            scheduler.stop_scheduler()

    asyncio.run(run())


@pytest.mark.parametrize(
    "day,time,timezone,now,expected",
    [
        (
            "mon",
            "10:15",
            "Asia/Shanghai",
            "2026-10-16T23:00:00+08:00",
            "2026-10-19T10:15:00+08:00",
        ),
        (
            "sun",
            "18:30",
            "UTC",
            "2026-10-16T23:00:00+00:00",
            "2026-10-18T18:30:00+00:00",
        ),
    ],
)
def test_weekly_schedule_uses_configured_day_time_and_timezone(
    monkeypatch, day, time, timezone, now, expected
):
    from reportagent.services import scheduler

    weekly = {
        "enabled": True,
        "day_of_week": day,
        "schedule_cron": time,
        "timezone": timezone,
    }
    monkeypatch.setattr(
        scheduler,
        "get_config",
        lambda key, **kwargs: weekly if key == "weekly_report" else {},
    )
    monkeypatch.setattr(scheduler, "_scheduler", None)

    async def run():
        scheduler.start_scheduler()
        try:
            job = scheduler._scheduler.get_job("feishu_weekly_report")
            assert (
                job.trigger.get_next_fire_time(
                    None, datetime.fromisoformat(now)
                ).isoformat()
                == expected
            )
        finally:
            scheduler.stop_scheduler()

    asyncio.run(run())


def test_app_config_schedules_weekly_report_friday_at_nine(monkeypatch):
    from reportagent.services import scheduler
    from reportagent.utils.config import load_config

    config = load_config()
    monkeypatch.setattr(
        scheduler, "get_config", lambda key, **kwargs: config.get(key, {})
    )
    monkeypatch.setattr(scheduler, "_scheduler", None)

    async def run():
        scheduler.start_scheduler()
        try:
            job = scheduler._scheduler.get_job("feishu_weekly_report")
            thursday = datetime(2026, 10, 15, 23, tzinfo=ZoneInfo("Asia/Shanghai"))
            assert (
                job.trigger.get_next_fire_time(None, thursday).isoformat()
                == "2026-10-16T09:00:00+08:00"
            )
        finally:
            scheduler.stop_scheduler()

    asyncio.run(run())


def test_weekly_job_runs_report_service_outside_the_scheduler_event_loop(monkeypatch):
    from reportagent.services import scheduler, weekly_report

    monkeypatch.setattr(
        scheduler,
        "get_config",
        lambda key, **kwargs: {"enabled": key == "weekly_report"},
    )
    monkeypatch.setattr(scheduler, "_scheduler", None)

    def generate(options):
        assert options == weekly_report.WeeklyReportOptions()
        # The real service uses asyncio.run for evidence preparation.
        return asyncio.run(asyncio.sleep(0, result={"status": "complete"}))

    monkeypatch.setattr(weekly_report, "run_weekly_report", generate)

    async def run():
        scheduler.start_scheduler()
        try:
            job = scheduler._scheduler.get_job("feishu_weekly_report")
            assert await job.func() == {"status": "complete"}
        finally:
            scheduler.stop_scheduler()

    asyncio.run(run())
