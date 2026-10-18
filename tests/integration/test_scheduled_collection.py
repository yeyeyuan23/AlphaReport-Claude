"""Scheduled ingestion shares the collector and persists truthful source status."""

import json

import pytest


@pytest.mark.parametrize("outcome", ["ok", "partial", "failed", "exception"])
def test_scheduled_collection_saves_source_results_without_generating_reports(
    tmp_path, monkeypatch, outcome
):
    from reportagent.services import scheduled_collection as jobs
    from reportagent.services import collection, collection_reviews
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        collection_reviews,
        "prepare_collection_reviews",
        AsyncMock(return_value={"review_ids": [], "failures": []}),
    )

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

    async def collect(options, **kwargs):
        assert [source.value for source in options.sources] == [
            "arxiv",
            "eastmoney",
            "bigquant",
        ]
        assert kwargs == {"no_analysis": True, "broad": True}
        assert options.max_results_per_source is None
        assert options.date_basis == "announcement"
        if outcome == "exception":
            raise RuntimeError("temporary source failure")
        return {"status": outcome, "source_statuses": statuses, "storage": {"added": 0}}

    monkeypatch.setattr(collection, "collect_reports", collect)
    result = jobs.run_scheduled_collection()
    saved = jobs.read_latest_collection()
    assert saved == result
    assert result["status"] == ("failed" if outcome == "exception" else outcome)
    assert result["completed_at"] and result["run_id"]
    assert result["elapsed_ms"] >= 0
    assert (
        "report_ids" not in result
    )  # This result is ingestion, not a report/send job.
    assert (
        json.loads(
            (tmp_path / "data/collections" / f"{result['run_id']}.json").read_text()
        )
        == result
    )


def test_scheduled_collection_reviews_after_ingestion_and_persists_failure(
    tmp_path, monkeypatch
):
    from reportagent.services import scheduled_collection as jobs, collection
    from reportagent.services import collection_reviews

    monkeypatch.setattr(jobs, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(jobs, "get_config", lambda *a, **kw: {"sources": ["arxiv"]})
    events = []

    async def collect(*args, **kwargs):
        events.append("ingested")
        return {
            "status": "ok",
            "source_statuses": [],
            "storage": {"added": 2},
            "report_ids": [1, 2],
        }

    async def review(**kwargs):
        assert events == ["ingested"]
        events.append("reviewed")
        return {"review_ids": [1, 2], "failures": [{"id": 2}], "deferred_ids": []}

    monkeypatch.setattr(collection, "collect_reports", collect)
    monkeypatch.setattr(collection_reviews, "prepare_collection_reviews", review)
    result = jobs.run_scheduled_collection()
    assert events == ["ingested", "reviewed"]
    assert result["status"] == "partial" and result["storage"]["added"] == 2
    assert result["review"]["failures"] == [{"id": 2}]
    assert result["review"]["elapsed_ms"] >= 0
    assert jobs.read_latest_collection() == result
