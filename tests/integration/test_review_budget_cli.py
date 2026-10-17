"""Report adapters leave review budgets optional and accept counts above twelve."""

from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("flow", ["daily", "weekly"])
@pytest.mark.parametrize(
    "arguments,expected", [([], None), (["--max-reviews", "30"], 30)]
)
def test_report_cli_preserves_optional_budget(monkeypatch, flow, arguments, expected):
    if flow == "daily":
        from reportagent.cli import daily_feishu_push as cli
        from reportagent.services import daily_push as service

        entry = "run_daily_push"
    else:
        from reportagent.cli import weekly_report as cli
        from reportagent.services import weekly_report as service

        entry = "run_weekly_report"
    run = Mock(return_value={"status": "preview"})
    monkeypatch.setattr(service, entry, run)
    monkeypatch.setattr("sys.argv", [flow, "--dry-run", *arguments])
    cli.main()
    assert run.call_args.args[0].max_reviews == expected
