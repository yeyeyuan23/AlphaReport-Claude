"""Independent daily ingestion and Feishu reporting jobs, hosted by the Web service."""

from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from reportagent.utils.config import get_config

logger = logging.getLogger(__name__)
_scheduler: AsyncIOScheduler | None = None


def _trigger(config: dict, default_time: str) -> CronTrigger:
    hour, minute = map(int, config.get("schedule_cron", default_time).split(":"))
    return CronTrigger(
        hour=hour,
        minute=minute,
        day_of_week="*",
        timezone=ZoneInfo(config.get("timezone", "Asia/Shanghai")),
    )


async def _collect():
    from reportagent.services.scheduled_collection import run_scheduled_collection

    return await asyncio.to_thread(run_scheduled_collection)


async def _report():
    from reportagent.services.daily_push import DailyPushOptions, run_daily_push

    return await asyncio.to_thread(run_daily_push, DailyPushOptions())


def start_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        return
    collection = get_config("daily_collection", default={})
    report = get_config("daily_report", default={})
    if not collection.get("enabled", False) and not report.get("enabled", False):
        logger.info("[scheduler] Collection and reporting disabled.")
        return
    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Shanghai"))
    if collection.get("enabled", False):
        scheduler.add_job(
            _collect,
            trigger=_trigger(collection, "08:00"),
            id="daily_collection",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
    if report.get("enabled", False):
        scheduler.add_job(
            _report,
            trigger=_trigger(report, "09:00"),
            id="feishu_daily_report",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
    scheduler.start()
    _scheduler = scheduler
    for job in scheduler.get_jobs():
        logger.info("[scheduler] %s next run: %s", job.id, job.next_run_time)


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def get_schedule_status() -> dict:
    collection = get_config("daily_collection", default={})
    report = get_config("daily_report", default={})
    running = _scheduler is not None and _scheduler.running
    collect_job = _scheduler.get_job("daily_collection") if running else None
    report_job = _scheduler.get_job("feishu_daily_report") if running else None
    return {
        "running": running,
        "config_enabled": collection.get("enabled", False)
        or report.get("enabled", False),
        "collection_enabled": collection.get("enabled", False),
        "report_enabled": report.get("enabled", False),
        "collection_next_run": collect_job.next_run_time.isoformat()
        if collect_job
        else None,
        "report_next_run": report_job.next_run_time.isoformat() if report_job else None,
    }
