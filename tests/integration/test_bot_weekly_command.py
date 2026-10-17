"""Weekly bot dispatch and worker boundaries, without models or real messages."""

import subprocess
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from reportagent.bot import feishu as bot
from reportagent.services import weekly_report as weekly


@pytest.fixture(autouse=True)
def isolated_job_lock(monkeypatch):
    monkeypatch.setattr(bot, "_report_lock", threading.Lock())


def test_weekly_starts_private_background_job_and_shares_report_lock(monkeypatch):
    thread = MagicMock()
    monkeypatch.setattr(bot.threading, "Thread", thread)
    assert "私聊" in (bot.handle_cmd("/weekly", "test-chat", "group") or "")
    thread.assert_not_called()
    reply = bot.handle_cmd("/weekly", "test-chat", "p2p")
    assert reply is not None and "开始" in reply and "UTC" in reply
    thread.assert_called_once_with(
        target=bot.run_weekly_job, args=("test-chat",), daemon=True
    )
    assert "正在" in bot.handle_cmd("/daily", "test-chat", "p2p")
    assert "正在" in bot.handle_cmd("/report 2005.13665", "test-chat", "p2p")
    assert thread.call_count == 1


def test_weekly_help_and_unexpected_arguments_do_not_fall_through_to_chat(monkeypatch):
    thread = MagicMock()
    monkeypatch.setattr(bot.threading, "Thread", thread)
    assert "/weekly" in bot.handle_cmd("/help", "test-chat", "p2p")
    assert "用法" in (bot.handle_cmd("/weekly other-target", "test-chat", "p2p") or "")
    thread.assert_not_called()
    assert not bot._report_lock.locked()


def test_weekly_thread_start_error_releases_the_shared_lock(monkeypatch):
    thread = MagicMock()
    thread.return_value.start.side_effect = RuntimeError("thread unavailable")
    monkeypatch.setattr(bot.threading, "Thread", thread)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        bot.handle_cmd("/weekly", "test-chat", "p2p")
    assert not bot._report_lock.locked()


@pytest.fixture
def worker(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        weekly,
        "get_previous_week_range",
        lambda: (
            datetime(2026, 10, 5, tzinfo=timezone.utc),
            datetime(2026, 10, 11, 23, 59, 59, tzinfo=timezone.utc),
            "2026-10-05",
            "2026-10-11",
        ),
    )
    monkeypatch.setattr(weekly, "get_weekly_papers", lambda *args: [{"id": 1}])
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    send = MagicMock()
    monkeypatch.setattr(bot.subprocess, "run", run)
    monkeypatch.setattr(bot, "send_chat", send)
    bot._report_lock.acquire()
    return run, send, tmp_path


def test_weekly_worker_uses_existing_cli_and_explicit_current_chat(worker, monkeypatch):
    run, send, root = worker
    monkeypatch.setenv("FEISHU_WEEKLY_PUSH_IDS", "different-configured-target")
    assert hasattr(bot, "run_weekly_job"), "Missing weekly worker"
    bot.run_weekly_job("current-chat")
    command = run.call_args.args[0]
    assert command[:4] == [
        bot.sys.executable,
        "-u",
        "-m",
        "reportagent.cli.weekly_report",
    ]
    assert command[command.index("--receive-ids") + 1] == "current-chat"
    assert command[command.index("--receive-type") + 1] == "chat_id"
    assert run.call_args.kwargs["cwd"] == str(root)
    assert run.call_args.kwargs.get("timeout") is None
    assert (root / "data/report-jobs.log").exists()
    send.assert_not_called()  # The existing CLI delivers the actual card.
    assert not bot._report_lock.locked()


def test_daily_worker_has_no_whole_job_deadline(worker):
    run, send, root = worker
    bot.run_daily_job("current-chat")
    command = run.call_args.args[0]
    assert command[3] == "reportagent.cli.daily_feishu_push"
    assert command[command.index("--receive-ids") + 1] == "current-chat"
    assert command[command.index("--receive-type") + 1] == "chat_id"
    assert run.call_args.kwargs["cwd"] == str(root)
    assert run.call_args.kwargs.get("timeout") is None
    send.assert_not_called()
    assert not bot._report_lock.locked()


def test_empty_week_notifies_user_and_does_not_call_models_or_delivery_cli(
    worker, monkeypatch
):
    run, send, _ = worker
    monkeypatch.setattr(weekly, "get_weekly_papers", lambda *args: [])
    assert hasattr(bot, "run_weekly_job"), "Missing weekly worker"
    bot.run_weekly_job("current-chat")
    run.assert_not_called()
    send.assert_called_once()
    text = send.call_args.args[1]
    assert "2026-10-05" in text and "2026-10-11" in text and "暂无" in text
    assert not bot._report_lock.locked()


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (subprocess.CompletedProcess([], 1), "未完整完成"),
        (subprocess.TimeoutExpired("weekly", 5400), "超时"),
        (OSError("local process error"), "未完成"),
    ],
)
def test_weekly_worker_reports_failure_and_always_releases_lock(
    worker, outcome, expected
):
    run, send, _ = worker
    if isinstance(outcome, Exception):
        run.side_effect = outcome
    else:
        run.return_value = outcome
    assert hasattr(bot, "run_weekly_job"), "Missing weekly worker"
    bot.run_weekly_job("current-chat")
    assert expected in send.call_args.args[1]
    assert not bot._report_lock.locked()


@pytest.mark.parametrize("command", ["/daily", "/weekly"])
def test_report_acknowledgement_does_not_promise_a_fixed_review_count(
    monkeypatch, command
):
    monkeypatch.setattr(bot.threading, "Thread", MagicMock())
    text = bot.handle_cmd(command, "test-chat", "p2p")
    assert "开始" in text
    assert "12" not in text
