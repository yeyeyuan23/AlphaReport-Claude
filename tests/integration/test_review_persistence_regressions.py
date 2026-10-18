import asyncio
import json
from unittest.mock import patch

import pytest

from reportagent.db import engine as database
from reportagent.models.database import Report
from reportagent.services import one_report, report_quality as quality
from tests.support.report_fixtures import Model, TEXT, paper, response


class PersistentModel(Model):
    async def close(self):
        pass

    def __init__(self):
        super().__init__(response())
        self._client = self


@pytest.fixture
def library(memory_session_factory):
    factory = memory_session_factory
    yield factory


def insert(factory, rid=1, **overrides):
    values = {
        "id": rid,
        "content_hash": str(rid),
        "arxiv_id": f"2005.1366{rid}v1",
        "authors": '["A"]',
    }
    values.update(overrides)
    values = paper(**values)
    with factory() as session:
        session.add(Report(**values))
        session.commit()


def test_configured_review_timeout_leaves_paper_retryable(library, monkeypatch):
    import copy
    from reportagent.utils import config

    settings = copy.deepcopy(config.load_config())
    settings["llm"]["full_text_review"] = {"timeout_seconds": 0.001}
    monkeypatch.setattr(config, "load_config", lambda: settings)
    insert(library)

    class SlowModel(PersistentModel):
        async def chat_json(self, messages, **kwargs):
            await asyncio.sleep(0.02)
            return await super().chat_json(messages, **kwargs)

    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch("reportagent.llm.client.LLMClient", return_value=SlowModel()),
    ):
        result = asyncio.run(quality.review_report_ids([1]))[1]
    assert result["status"] == "pending"
    assert "TimeoutError" in result["validation_issues"][0]


@pytest.mark.parametrize("cache", ["{broken", "[]", '"not object"'])
def test_malformed_cache_does_not_block_sibling_reviews(library, cache):
    insert(library, 1, evaluation_json=cache)
    insert(library, 2)
    llm = PersistentModel()
    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch("reportagent.llm.client.LLMClient", return_value=llm),
    ):
        results = asyncio.run(quality.review_report_ids([1, 2]))
    assert set(results) == {1, 2}
    assert all(value["status"] == "reviewed" for value in results.values())
    assert len(llm.requests) == 2


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize(
    "full_text,has_full_text",
    [
        (None, False),
        ("<html><body>Service unavailable</body></html>", True),
        (TEXT, False),
    ],
    ids=["missing", "unusable", "incomplete"],
)
def test_missing_material_never_fetches_or_changes_paper_and_preserves_sibling_budget(
    library, full_text, has_full_text, force
):
    insert(library, 1, full_text=full_text, has_full_text=has_full_text)
    insert(library, 2)
    with library() as session:
        row = session.get(Report, 1)
        before = {
            column.name: getattr(row, column.name)
            for column in Report.__table__.columns
            if column.name not in {"evaluation_json", "updated_at"}
        }
    llm = PersistentModel()
    audit = {}
    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch("reportagent.llm.client.LLMClient", return_value=llm),
        patch.object(one_report, "collect_paper") as fetch,
    ):
        results = asyncio.run(
            quality.review_report_ids([1, 2], force=force, max_reviews=1, audit=audit)
        )
    fetch.assert_not_awaited()
    assert results[1]["status"] == "pending"
    assert results[2]["status"] == "reviewed"
    assert audit["pending_material_ids"] == [1]
    assert audit["review_ids"] == [2]
    assert len(llm.requests) == 1
    with library() as session:
        row = session.get(Report, 1)
        assert {name: getattr(row, name) for name in before} == before


def test_model_result_cannot_overwrite_concurrent_screening_change(library):
    insert(library)
    llm = PersistentModel()
    original_call = llm.chat_json

    async def changed(messages, **kwargs):
        with library() as session:
            session.get(Report, 1).selection_relevance = 1
            session.commit()
        return await original_call(messages, **kwargs)

    llm.chat_json = changed
    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch("reportagent.llm.client.LLMClient", return_value=llm),
    ):
        results = asyncio.run(quality.review_report_ids([1]))
    assert results[1]["tier"] == "pending"
    with library() as session:
        assert session.get(Report, 1).evaluation_json is None


def test_failed_forced_review_preserves_previous_successful_saved_evidence(library):
    previous = asyncio.run(quality.evaluate_paper(paper(), PersistentModel()))
    insert(library, evaluation_json=json.dumps(previous))
    llm = PersistentModel()

    async def failed(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    llm.chat_json = failed
    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch("reportagent.llm.client.LLMClient", return_value=llm),
    ):
        results = asyncio.run(quality.review_report_ids([1], force=True))
    assert results[1]["tier"] == "pending"
    with library() as session:
        assert json.loads(session.get(Report, 1).evaluation_json) == previous


def test_old_quote_screening_cache_is_refreshed_without_requiring_force(library):
    insert(library)
    llm = PersistentModel()
    llm.model = quality.get_llm_model()
    llm.data["findings"] = [
        {
            "claim": "作者报告的结论及适用条件",
            "quote": "An excerpt with different PDF typesetting.",
            "chunk_id": "C002",
        }
    ]
    with library() as session:
        row = session.get(Report, 1)
        with patch.object(quality, "SCHEMA_VERSION", 3):
            old_fingerprint = quality.review_fingerprint(vars(row), llm.model)
        row.evaluation_json = json.dumps(
            {
                **response(),
                "schema_version": 3,
                "fingerprint": old_fingerprint,
                "status": "reviewed",
                "tier": "browse",
            }
        )
        session.commit()
    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch("reportagent.llm.client.LLMClient", return_value=llm),
    ):
        result = asyncio.run(quality.review_report_ids([1]))[1]
    assert result["tier"] == "focus"
    assert result["findings"][0]["claim"] == "作者报告的结论及适用条件"
    assert not result["validation_issues"]
    with library() as session:
        assert json.loads(session.get(Report, 1).evaluation_json) == result
