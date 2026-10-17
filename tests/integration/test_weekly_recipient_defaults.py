"""Weekly delivery must use the ID type belonging to its recipient source."""

import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from reportagent.cli import weekly_report as weekly_cli
from reportagent.services import weekly_report as weekly


@pytest.mark.parametrize(
    "environment,cli_args,expected_id,expected_type",
    [
        pytest.param(
            {"FEISHU_DAILY_PUSH_IDS": "daily-open"},
            None, "daily-open", "open_id",
            id="daily-recipient-defaults-to-open-id",
        ),
        pytest.param(
            {"FEISHU_DAILY_PUSH_IDS": "daily-chat", "FEISHU_DAILY_PUSH_TYPE": "chat_id"},
            None, "daily-chat", "chat_id",
            id="daily-recipient-keeps-explicit-type",
        ),
        pytest.param(
            {"FEISHU_WEEKLY_PUSH_IDS": "weekly-chat", "FEISHU_DAILY_PUSH_IDS": "daily-open"},
            None, "weekly-chat", "chat_id",
            id="weekly-recipient-defaults-to-chat-id",
        ),
        pytest.param(
            {"FEISHU_WEEKLY_PUSH_IDS": "weekly-open", "FEISHU_WEEKLY_PUSH_TYPE": "open_id"},
            None, "weekly-open", "open_id",
            id="weekly-recipient-keeps-explicit-type",
        ),
        pytest.param(
            {"FEISHU_WEEKLY_PUSH_IDS": "configured-open", "FEISHU_WEEKLY_PUSH_TYPE": "open_id"},
            ["--receive-ids", "cli-chat", "--receive-type", "chat_id"],
            "cli-chat", "chat_id",
            id="cli-recipient-and-type-override-environment",
        ),
    ],
)
def test_weekly_delivery_uses_recipient_type(
    monkeypatch, tmp_path, environment, cli_args, expected_id, expected_type,
):
    # A developer's .env must not supply a type and hide a broken fallback.
    for name in (
        "FEISHU_WEEKLY_PUSH_IDS", "FEISHU_WEEKLY_PUSH_TYPE",
        "FEISHU_DAILY_PUSH_IDS", "FEISHU_DAILY_PUSH_TYPE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    papers = [{"id": 1, "title": "Offline weekly recipient fixture"}]
    monkeypatch.setattr(weekly, "get_previous_week_range", lambda *args: (
        datetime(2026, 10, 5, tzinfo=timezone.utc),
        datetime(2026, 10, 11, 23, 59, 59, tzinfo=timezone.utc),
        "2026-10-05", "2026-10-11",
    ))
    monkeypatch.setattr(weekly, "get_weekly_papers", lambda *args: papers)
    monkeypatch.setattr(weekly, "generate_weekly_report", lambda *args: "Offline weekly report")
    prepare = AsyncMock(side_effect=AssertionError("No model refresh in recipient tests"))
    send = MagicMock(return_value=True)
    monkeypatch.setattr(weekly, "prepare_weekly_evidence", prepare)
    monkeypatch.setattr(weekly, "send_feishu_card", send)
    output = tmp_path / "weekly.md"

    if cli_args is None:
        result = weekly.run_weekly_report(weekly.WeeklyReportOptions(
            no_llm=True, output=str(output),
        ))
        assert result["status"] == "complete"
        assert result["sent"] == 1
        assert result["failed_recipients"] == []
    else:
        monkeypatch.setattr(sys, "argv", [
            "weekly_report", "--no-llm", "--output", str(output), *cli_args,
        ])
        weekly_cli.main()

    send.assert_called_once_with(
        expected_id, "Offline weekly report", papers,
        "2026-10-05", "2026-10-11", expected_type,
    )
    prepare.assert_not_awaited()
    assert output.read_text(encoding="utf-8") == "Offline weekly report"
