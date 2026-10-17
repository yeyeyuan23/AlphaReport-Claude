"""Scheduled ingestion shares the collector and persists truthful source status."""

import json

import pytest


@pytest.mark.parametrize("outcome", ["ok", "partial", "failed", "exception"])
def test_scheduled_collection_saves_source_results_without_generating_reports(
    tmp_path, monkeypatch, outcome
):
    from reportagent.services import scheduled_collection as jobs
    from reportagent.services import collection

    monkeypatch.setattr(jobs, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda *args, **kwargs: {
            "sources": ["arxiv", "eastmoney", "bigquant"],
            "lookback_days": 3,
            "max_per_source": 15,
            "topics": "量化金融",
        },
    )
    statuses = [
        {
            "source": "arxiv",
            "status": "error" if outcome == "failed" else "ok",
            "valid": 0,
        }
    ]

    async def collect(options):
        assert options.sources == "arxiv,eastmoney,bigquant"
        assert options.no_analysis is True and options.broad is True
        assert options.days == 3 and options.max_results == 15
        if outcome == "exception":
            raise RuntimeError("temporary source failure")
        return {"status": outcome, "source_statuses": statuses, "storage": {"added": 0}}

    monkeypatch.setattr(collection, "collect_reports", collect)
    result = jobs.run_scheduled_collection()
    saved = jobs.read_latest_collection()
    assert saved == result
    assert result["status"] == ("failed" if outcome == "exception" else outcome)
    assert result["completed_at"] and result["run_id"]
    assert (
        "report_ids" not in result
    )  # This result is ingestion, not a report/send job.
    assert (
        json.loads(
            (tmp_path / "data/collections" / f"{result['run_id']}.json").read_text()
        )
        == result
    )
