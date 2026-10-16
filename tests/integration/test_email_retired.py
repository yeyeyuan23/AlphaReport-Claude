"""Retired mail delivery must be unreachable without removing research tools."""

import importlib
import asyncio
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["reportagent.main", "reportagent.api.legacy_app"])
def test_web_entrypoints_do_not_register_mail_delivery(module):
    app = importlib.import_module(module).app
    paths = set(app.openapi()["paths"])
    assert "/api/v1/reports" in paths
    assert not any("/subscriptions" in path or "/email" in path for path in paths)


def test_chat_catalog_keeps_research_tools_without_mail_actions():
    from reportagent.chat.tools import ALL_TOOLS

    names = {tool.name for tool in ALL_TOOLS}
    assert {"semantic_search_reports", "brainstorm_research", "manage_skill"} <= names
    assert not {"save_report_direction", "generate_daily_report"} & names


def test_disabled_scheduler_operates_without_subscription_service():
    # A fresh process catches import-time dependencies and prevents a cached
    # module from hiding a dependency on removed subscription persistence.
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
sys.modules['reportagent.services.email_settings'] = None
from reportagent.services import scheduler
scheduler.get_config = lambda *args, **kwargs: {'enabled': False}
scheduler.start_scheduler()
status = scheduler.get_schedule_status()
assert status['running'] is False
assert status['config_enabled'] is False
assert 'subscriptions' not in status
"""],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_legacy_collection_schedule_keeps_arxiv_without_mail_fallback(monkeypatch):
    from reportagent.services import scheduler

    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "get_config", lambda *args, **kwargs: {
        "enabled": True,
        "schedule_cron": "23:59",
        "arxiv_collection": {"enabled": True, "schedule_cron": "23:59"},
    })

    async def check():
        scheduler.start_scheduler()
        try:
            assert {job.id for job in scheduler._scheduler.get_jobs()} == {"arxiv_daily_collect"}
        finally:
            scheduler.stop_scheduler()

    asyncio.run(check())
