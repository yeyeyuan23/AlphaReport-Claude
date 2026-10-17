import asyncio
import json
from unittest.mock import patch

import pytest

from reportagent.db import engine as database
from reportagent.models.database import Report
from reportagent.models.schemas import SearchResult, SourceType
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


def test_fetched_revision_updates_coherent_metadata_and_clears_old_screening(library):
    insert(
        library,
        title="Version 1",
        full_text=None,
        has_full_text=False,
        arxiv_id="2005.13661",
    )
    fetched = SearchResult(
        title="Version 2",
        authors=["New author"],
        abstract="New abstract",
        full_text=TEXT,
        source=SourceType.ARXIV,
        arxiv_id="2005.13661v2",
        source_url="https://arxiv.org/abs/2005.13661v2",
    )
    llm = PersistentModel()
    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch("reportagent.llm.client.LLMClient", return_value=llm),
        patch.object(one_report, "collect_paper", return_value=fetched),
    ):
        results = asyncio.run(quality.review_report_ids([1]))
    user_message = json.loads(llm.requests[0][1]["content"])
    assert user_message["title"] == "Version 2"
    assert results[1]["tier"] == "pending"
    assert results[1]["relevance"] is None
    with library() as session:
        row = session.get(Report, 1)
        assert row.title == "Version 2"
        assert json.loads(row.authors) == ["New author"]
        assert row.selection_provenance_json is None
        assert row.selection_relevance is None


def test_fetch_cannot_overwrite_a_concurrent_new_revision(library):
    insert(library, full_text=None, has_full_text=False, arxiv_id="2005.13661")

    async def fetch(_):
        with library() as session:
            row = session.get(Report, 1)
            row.arxiv_id, row.full_text, row.has_full_text = (
                "2005.13661v3",
                TEXT + "New text",
                True,
            )
            session.commit()
        return SearchResult(
            title="Old fetch",
            source=SourceType.ARXIV,
            full_text=TEXT,
            arxiv_id="2005.13661v2",
        )

    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch.object(one_report, "collect_paper", side_effect=fetch),
        patch("reportagent.llm.client.LLMClient", return_value=PersistentModel()),
    ):
        results = asyncio.run(quality.review_report_ids([1]))
    assert results[1]["tier"] == "pending"
    with library() as session:
        assert session.get(Report, 1).arxiv_id == "2005.13661v3"
        assert session.get(Report, 1).evaluation_json is None


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


def test_fetching_same_revision_full_text_preserves_current_abstract_relevance(library):
    from reportagent.services.paper_scoring import screening_is_current

    insert(library, full_text=None, has_full_text=False)
    with library() as session:
        row = session.get(Report, 1)
        before_provenance = row.selection_provenance_json
        before_review_fingerprint = quality.review_fingerprint(
            vars(row), "fixture-model"
        )
        fetched = SearchResult(
            title=row.title,
            authors=["A"],
            abstract=row.abstract,
            arxiv_id=row.arxiv_id,
            source=SourceType.ARXIV,
            full_text=TEXT,
            source_url=f"https://arxiv.org/abs/{row.arxiv_id}",
        )
        assert screening_is_current(vars(row))
    llm = PersistentModel()
    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch("reportagent.llm.client.LLMClient", return_value=llm),
        patch.object(one_report, "collect_paper", return_value=fetched),
    ):
        results = asyncio.run(quality.review_report_ids([1]))
    assert results[1]["tier"] == "focus"
    assert results[1]["status"] == "reviewed"
    assert results[1]["relevance"] == 8
    with library() as session:
        row = session.get(Report, 1)
        assert row.has_full_text and row.full_text == TEXT
        assert row.selection_provenance_json == before_provenance
        assert screening_is_current(vars(row))
        assert (
            quality.review_fingerprint(vars(row), "fixture-model")
            != before_review_fingerprint
        )
        assert json.loads(row.evaluation_json)["tier"] == "focus"


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


def test_hydration_preserves_known_publication_date(library):
    from datetime import datetime

    known = datetime(2020, 5, 27)
    insert(library, full_text=None, has_full_text=False, published_date=known)
    with library() as session:
        row = session.get(Report, 1)
        fetched = SearchResult(
            title=row.title,
            abstract=row.abstract,
            authors=["A"],
            source=SourceType.ARXIV,
            arxiv_id=row.arxiv_id,
            full_text=TEXT,
            published_date=None,
        )
    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch.object(one_report, "collect_paper", return_value=fetched),
        patch("reportagent.llm.client.LLMClient", return_value=PersistentModel()),
    ):
        results = asyncio.run(quality.review_report_ids([1]))
    assert results[1]["status"] == "reviewed"
    with library() as session:
        assert session.get(Report, 1).published_date == known


def test_hydration_cannot_downgrade_an_existing_arxiv_revision(library):
    insert(library, arxiv_id="2005.13661v3", full_text=None, has_full_text=False)
    fetched = SearchResult(
        title="Stale version",
        authors=["Old author"],
        full_text=TEXT,
        source=SourceType.ARXIV,
        arxiv_id="2005.13661v2",
    )
    llm = PersistentModel()
    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch.object(one_report, "collect_paper", return_value=fetched),
        patch("reportagent.llm.client.LLMClient", return_value=llm),
    ):
        result = asyncio.run(quality.review_report_ids([1]))[1]
    assert result["tier"] == "pending"
    assert not llm.requests
    with library() as session:
        row = session.get(Report, 1)
        assert row.arxiv_id == "2005.13661v3"
        assert row.full_text is None
        assert row.title == "Portfolio research"


def test_hydration_cannot_overwrite_concurrent_classification_edit(library):
    insert(library, full_text=None, has_full_text=False)

    async def fetch(_):
        with library() as session:
            row = session.get(Report, 1)
            row.classification_method = "hybrid"
            row.classification_provenance_json = '{"concurrent":true}'
            session.commit()
        return SearchResult(
            title="Portfolio research",
            abstract="Portfolio allocation",
            full_text=TEXT,
            source=SourceType.ARXIV,
            arxiv_id="2005.13661v1",
        )

    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch.object(one_report, "collect_paper", side_effect=fetch),
        patch("reportagent.llm.client.LLMClient", return_value=PersistentModel()),
    ):
        result = asyncio.run(quality.review_report_ids([1]))[1]
    assert result["tier"] == "pending"
    with library() as session:
        row = session.get(Report, 1)
        assert row.classification_method == "hybrid"
        assert row.classification_provenance_json == '{"concurrent":true}'
        assert row.full_text is None
        assert row.evaluation_json is None


@pytest.mark.parametrize("revision", ["2005.13661v1", "2005.13661v2"])
def test_hydration_preserves_hybrid_labels_for_unchanged_classifier_input(
    library, revision
):
    from reportagent.classifiers.provenance import input_fingerprint
    from reportagent.db.repository import ReportRepository
    from reportagent.models.schemas import ClassificationResult, Topic

    insert(library, full_text=None, has_full_text=False)
    with library() as session:
        row = session.get(Report, 1)
        provenance = {
            "schema_version": 1,
            "input_sha256": input_fingerprint(row.title, row.abstract),
            "labels": {
                "topics.portfolio_optimization": {
                    "source": "title",
                    "quote": "Portfolio",
                    "reason": "Portfolio target",
                    "raw_start": 0,
                    "raw_end": 9,
                }
            },
            "rejected_labels": [],
        }
        ReportRepository(session).apply_classification(
            row,
            ClassificationResult(
                topics=[Topic.PORTFOLIO_OPT],
                confidence=0.9,
                method="hybrid",
                provenance=provenance,
            ),
        )
        session.commit()
        before = row.classification_provenance_json
        tag_ids = [tag.id for tag in row.tags]
        fetched = SearchResult(
            title=row.title,
            abstract=row.abstract,
            authors=["A"],
            source=SourceType.ARXIV,
            arxiv_id=revision,
            full_text=TEXT,
        )
    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch.object(one_report, "collect_paper", return_value=fetched),
        patch("reportagent.llm.client.LLMClient", return_value=PersistentModel()),
    ):
        asyncio.run(quality.review_report_ids([1]))
    with library() as session:
        row = session.get(Report, 1)
        assert row.full_text == TEXT
        assert row.classification_method == "hybrid"
        assert row.classification_confidence == 0.9
        assert row.topics == "portfolio_optimization"
        assert row.classification_provenance_json == before
        assert [tag.id for tag in row.tags] == tag_ids


def test_hydration_refreshes_rules_from_canonical_abstract_not_body_mentions(library):
    insert(library, full_text=None, has_full_text=False, topics="factor_model")
    body = (
        "machine learning deep learning neural network transformer GNN LSTM reinforcement learning GPT LLM "
        * 20
    )
    fetched = SearchResult(
        title="A neutral research paper",
        abstract="We examine research decisions.",
        full_text=body,
        source=SourceType.ARXIV,
        arxiv_id="2005.13661v2",
    )
    with (
        patch.object(database, "get_session_factory", return_value=library),
        patch.object(one_report, "collect_paper", return_value=fetched),
        patch("reportagent.llm.client.LLMClient", return_value=PersistentModel()),
    ):
        asyncio.run(quality.review_report_ids([1]))
    with library() as session:
        row = session.get(Report, 1)
        assert row.topics == ""
        assert row.classification_method == "rule"
        assert row.classification_provenance_json is None
