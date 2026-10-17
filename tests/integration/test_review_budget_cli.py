"""Report adapters expose no model execution controls."""

from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("flow", ["daily", "weekly"])
@pytest.mark.parametrize(
    "arguments", [[], ["--max-reviews", "30"], ["--force-review"], ["--no-llm"]]
)
def test_report_cli_rejects_obsolete_review_flags(monkeypatch, flow, arguments):
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
    if arguments:
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
        run.assert_not_called()
    else:
        cli.main()
        assert run.call_args.args[0].dry_run
        assert not hasattr(run.call_args.args[0], "max_reviews")
