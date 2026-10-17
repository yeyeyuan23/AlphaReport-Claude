"""All collection entries share selection, exact criteria and truthful cancellation."""

import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from reportagent.models.schemas import SearchResult, SourceType, UserCriteria
from reportagent.services import collection
from tests.support.collection_scoring import RelevanceModel


@pytest.fixture
def library(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'reports.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    from reportagent.db import engine as db_engine

    monkeypatch.setattr(db_engine, "_SessionLocal", factory)
    monkeypatch.setattr(db_engine, "_engine", engine)
    monkeypatch.setattr(collection, "init_db", lambda: None)
    monkeypatch.setattr(collection, "get_session_factory", lambda: factory)
    from reportagent.services import batch_analysis

    monkeypatch.setattr(batch_analysis, "init_db", lambda: None)
    monkeypatch.setattr(batch_analysis, "get_session_factory", lambda: factory)
    monkeypatch.setattr(collection, "LLMClient", RelevanceModel)
    monkeypatch.setattr(collection, "LLMClassifier", lambda _: None)
    yield factory
    engine.dispose()


def paper(number=1):
    return SearchResult(
        title=f"Quantitative portfolio optimization risk model {number}",
        abstract="We evaluate portfolio covariance and financial risk using market returns.",
        source=SourceType.ARXIV,
        arxiv_id=f"2610.1000{number}v1",
        abstract_only=True,
    )


def source(search, kind=SourceType.ARXIV, **kwargs):
    return SimpleNamespace(
        source_type=kind, is_available=lambda: True, search=search, **kwargs
    )


def test_criteria_keep_precise_times_lists_and_directory(
    library, monkeypatch, tmp_path
):
    criteria = UserCriteria(
        topics=["portfolio, allocation", "risk"],
        keywords=["mean, variance"],
        sources=[SourceType.ARXIV],
        journals=["Journal, A"],
        brokers=["Broker A"],
        date_from=datetime(
            2026, 10, 1, 13, 14, 15, 1234, tzinfo=timezone(timedelta(hours=8))
        ),
        date_to=datetime(
            2026, 10, 2, 15, 16, 17, 1234, tzinfo=timezone(timedelta(hours=8))
        ),
        local_pdf_path=str(tmp_path / "custom, library"),
    )

    async def search(actual):
        assert actual.model_dump() == criteria.model_dump()
        return [paper()]

    monkeypatch.setattr(collection, "build_sources", lambda _: [source(search)])
    result = asyncio.run(collection.collect_reports(criteria, no_analysis=True))
    with library() as session:
        row = session.query(Report).one()
        assert result["report_ids"] == [row.id]
        assert row.title == "Quantitative portfolio optimization risk model 1"
    assert result["topics"] == ["portfolio, allocation", "risk"]
    timing = result["timing"]
    assert timing["started_at"] <= timing["completed_at"]
    assert timing["elapsed_ms"] >= 0
    assert {
        "metadata",
        "relevance",
        "classification",
        "storage",
        "download_parse",
    } <= timing["stages_ms"].keys()
    assert all(ms >= 0 for ms in timing["stages_ms"].values())
    assert result["source_statuses"][0]["timing"]["stages_ms"] == timing["stages_ms"]


def test_cancellation_after_scoring_prevents_persistence_and_download(
    library, monkeypatch
):
    cancelled = False

    class Model(RelevanceModel):
        async def chat_json(self, *args, **kwargs):
            nonlocal cancelled
            value = await super().chat_json(*args, **kwargs)
            cancelled = True
            return value

    async def search(_):
        return [paper()]

    async def hydrate(_):
        pytest.fail("cancelled screening must never download")

    monkeypatch.setattr(collection, "LLMClient", Model)
    monkeypatch.setattr(
        collection, "build_sources", lambda _: [source(search, hydrate_result=hydrate)]
    )
    result = asyncio.run(
        collection.collect_reports(
            UserCriteria(topics=["portfolio"], sources=[SourceType.ARXIV]),
            no_analysis=True,
            cancel_check=lambda: cancelled,
        )
    )
    assert result["status"] == "cancelled" and result["report_ids"] == []
    with library() as session:
        assert session.query(Report).count() == 0


def test_cancelled_partial_collection_reports_committed_rows_and_skips_analysis(
    library, monkeypatch
):
    from reportagent.services import batch_analysis

    cancelled = False

    async def search(_):
        return [paper(1), paper(2)]

    async def hydrate(result):
        nonlocal cancelled
        cancelled = True
        return result.model_copy(
            update={
                "full_text": "A portfolio risk model is estimated using market returns. "
                * 20,
                "abstract_only": False,
            }
        )

    async def analyze(*args, **kwargs):
        pytest.fail("cancelled collection must never analyze")

    monkeypatch.setattr(batch_analysis, "run_analysis", analyze)
    monkeypatch.setattr(
        collection, "build_sources", lambda _: [source(search, hydrate_result=hydrate)]
    )
    result = asyncio.run(
        collection.collect_reports(
            UserCriteria(topics=["portfolio"], sources=[SourceType.ARXIV]),
            cancel_check=lambda: cancelled,
        )
    )
    assert result["status"] == "cancelled" and result["results_count"] == 1
    assert result["storage"]["analyzed"] == 0
    with library() as session:
        rows = session.query(Report).all()
        assert [row.id for row in rows] == result["report_ids"]
        assert rows[0].full_text is None


def test_local_override_directory_is_available_even_if_default_is_missing(
    library, monkeypatch, tmp_path
):
    from reportagent.sources.local_pdf import LocalPDFSource

    override = tmp_path / "selected-library"
    override.mkdir()
    local = LocalPDFSource(str(tmp_path / "missing-default"))
    calls = []

    async def search(criteria):
        calls.append(criteria.local_pdf_path)
        return [paper().model_copy(update={"source": SourceType.LOCAL_PDF})]

    monkeypatch.setattr(local, "search", search)
    monkeypatch.setattr(collection, "build_sources", lambda _: [local])
    result = asyncio.run(
        collection.collect_reports(
            UserCriteria(
                topics=["portfolio"],
                sources=[SourceType.LOCAL_PDF],
                local_pdf_path=str(override),
            ),
            no_analysis=True,
        )
    )
    assert result["results_count"] == 1 and calls == [str(override)]


def test_partial_source_failure_preserves_saved_receipt_and_safe_progress(
    library, monkeypatch
):
    async def good(_):
        return [paper()]

    async def broken(_):
        raise RuntimeError("secret internal source details")

    monkeypatch.setattr(
        collection,
        "build_sources",
        lambda _: [source(good), source(broken, SourceType.BIGQUANT)],
    )
    progress = []
    result = asyncio.run(
        collection.collect_reports(
            UserCriteria(
                topics=["portfolio"], sources=[SourceType.ARXIV, SourceType.BIGQUANT]
            ),
            no_analysis=True,
            progress_cb=lambda phase, message: progress.append((phase, message)),
        )
    )
    assert result["status"] == "partial"
    assert result["results_count"] == 1 and len(result["report_ids"]) == 1
    with library() as session:
        assert session.query(Report).one().id == result["report_ids"][0]
    assert result["storage"]["added"] == 1
    assert progress[-1][0] == "partial"
    assert "secret internal" not in str(result) + str(progress)
