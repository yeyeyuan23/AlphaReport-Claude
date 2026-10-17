"""Old classification requests may not replace changed input or label state."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report, Tag
from reportagent.models.schemas import ClassificationResult, Topic
from reportagent.services import batch_classification as batch


@pytest.fixture
def classification_library(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'library.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        row = Report(
            title="Original title",
            abstract="Original abstract",
            source="arxiv",
            content_hash="paper",
            topics="portfolio_optimization",
        )
        row.tags = [
            Tag(dimension="topic", value="portfolio_optimization"),
            Tag(dimension="custom", value="original"),
        ]
        db.add(row)
        db.commit()
    monkeypatch.setattr(batch, "init_db", lambda: None)
    monkeypatch.setattr(batch, "get_session_factory", lambda: factory)
    monkeypatch.setattr(batch.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        batch,
        "LLMClient",
        lambda: SimpleNamespace(_client=SimpleNamespace(close=AsyncMock())),
    )
    yield factory
    engine.dispose()


def snapshot(row):
    return {
        **{
            field: getattr(row, field)
            for field in (
                "title",
                "abstract",
                "markets",
                "asset_classes",
                "frequencies",
                "topics",
                "classification_confidence",
                "classification_method",
                "classification_provenance_json",
            )
        },
        "tags": sorted((tag.dimension, tag.value) for tag in row.tags),
    }


@pytest.mark.parametrize(
    "change", ["title", "abstract", "topics", "provenance", "tags"]
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_stale_classification_preserves_concurrent_state(
    classification_library, monkeypatch, change, dry_run
):
    updated = {}

    async def classify(*_):
        with classification_library() as db:
            row = db.get(Report, 1)
            if change == "provenance":
                row.classification_provenance_json = '{"new": "evidence"}'
            elif change == "tags":
                row.tags = [Tag(dimension="custom", value="new user label")]
            else:
                setattr(row, change, "new current value")
            db.commit()
            updated.update(snapshot(row))
        return ClassificationResult(topics=[Topic.FACTOR_MODEL], method="llm")

    monkeypatch.setattr(
        batch, "LLMClassifier", lambda _: SimpleNamespace(classify=classify)
    )
    audit = asyncio.run(
        batch.classify_async(
            batch.ClassificationOptions(report_id=1, llm=True, dry_run=dry_run)
        )
    )
    assert audit["failed_ids"] == [1]
    assert audit["classified_ids"] == []
    assert audit["updated"] == 0
    with classification_library() as db:
        assert snapshot(db.get(Report, 1)) == updated


def test_classification_deleted_during_model_call_is_not_success(
    classification_library, monkeypatch
):
    async def classify(*_):
        with classification_library() as db:
            db.delete(db.get(Report, 1))
            db.commit()
        return ClassificationResult(topics=[Topic.FACTOR_MODEL], method="llm")

    monkeypatch.setattr(
        batch, "LLMClassifier", lambda _: SimpleNamespace(classify=classify)
    )
    audit = asyncio.run(
        batch.classify_async(batch.ClassificationOptions(report_id=1, llm=True))
    )
    assert audit["failed_ids"] == [1]
    assert audit["classified"] == 0
    with classification_library() as db:
        assert db.get(Report, 1) is None


def test_simultaneous_classifications_have_one_writer_and_no_model_time_connection(
    classification_library, monkeypatch
):
    async def run():
        ready = asyncio.Event()
        started = 0

        async def classify(*_):
            nonlocal started
            assert classification_library.kw["bind"].pool.checkedout() == 0
            started += 1
            if started == 2:
                ready.set()
            await ready.wait()
            return ClassificationResult(topics=[Topic.FACTOR_MODEL], method="llm")

        monkeypatch.setattr(
            batch, "LLMClassifier", lambda _: SimpleNamespace(classify=classify)
        )
        options = batch.ClassificationOptions(report_id=1, llm=True)
        return await asyncio.gather(
            batch.classify_async(options), batch.classify_async(options)
        )

    audits = asyncio.run(run())
    assert sum(audit["classified"] for audit in audits) == 1
    assert sum(audit["failed"] for audit in audits) == 1
    assert sum(audit["updated"] for audit in audits) == 1
    with classification_library() as db:
        row = db.get(Report, 1)
        assert row.topics == "factor_model"
        assert [(tag.dimension, tag.value) for tag in row.tags] == [
            ("topic", "factor_model")
        ]


def test_read_failure_does_not_abort_other_selected_papers(
    classification_library, monkeypatch
):
    with classification_library() as db:
        db.add(
            Report(
                title="Second",
                abstract="Current abstract",
                source="arxiv",
                content_hash="second",
            )
        )
        db.commit()
    real_get = classification_library.class_.get
    failed = False

    def fail_first_read(session, model, ident, **kwargs):
        nonlocal failed
        if model is Report and ident == 1 and not failed:
            failed = True
            raise RuntimeError("temporary read failure")
        return real_get(session, model, ident, **kwargs)

    monkeypatch.setattr(classification_library.class_, "get", fail_first_read)
    monkeypatch.setattr(
        batch,
        "LLMClassifier",
        lambda _: SimpleNamespace(
            classify=AsyncMock(
                return_value=ClassificationResult(
                    topics=[Topic.FACTOR_MODEL], method="llm"
                )
            )
        ),
    )
    audit = asyncio.run(
        batch.classify_async(batch.ClassificationOptions(all=True, llm=True))
    )
    assert audit["failed_ids"] == [1]
    assert audit["classified_ids"] == [2]
    assert audit["updated"] == 1


def test_guard_holds_a_transaction_through_label_application(
    classification_library, monkeypatch
):
    dry_run = False
    from sqlalchemy import text, update
    from sqlalchemy.exc import OperationalError
    from reportagent.db.repository import ReportRepository

    apply = ReportRepository.apply_classification
    blocked = []

    def apply_with_competing_edit(repo, row, result, **kwargs):
        with classification_library() as other:
            other.execute(text("PRAGMA busy_timeout=0"))
            try:
                other.execute(
                    update(Report)
                    .where(Report.id == row.id)
                    .values(abstract="racing edit")
                )
                other.commit()
                blocked.append(False)
            except OperationalError as error:
                assert "locked" in str(error)
                blocked.append(True)
        return apply(repo, row, result, **kwargs)

    monkeypatch.setattr(
        ReportRepository, "apply_classification", apply_with_competing_edit
    )
    monkeypatch.setattr(
        batch,
        "LLMClassifier",
        lambda _: SimpleNamespace(
            classify=AsyncMock(
                return_value=ClassificationResult(
                    topics=[Topic.FACTOR_MODEL], method="llm"
                )
            )
        ),
    )
    audit = asyncio.run(
        batch.classify_async(
            batch.ClassificationOptions(report_id=1, llm=True, dry_run=dry_run)
        )
    )
    assert audit["classified_ids"] == [1]
    assert blocked == [True]
    with classification_library() as db:
        row = db.get(Report, 1)
        assert row.abstract == "Original abstract"
        assert row.topics == ("portfolio_optimization" if dry_run else "factor_model")


def test_persistence_failure_rolls_back_only_its_paper(
    classification_library, monkeypatch
):
    from reportagent.db.repository import ReportRepository

    with classification_library() as db:
        db.add(
            Report(
                title="Second",
                abstract="Current abstract",
                source="arxiv",
                content_hash="second",
            )
        )
        db.commit()
    apply = ReportRepository.apply_classification

    def fail_first_write(repo, row, result, **kwargs):
        changed = apply(repo, row, result, **kwargs)
        if row.id == 1:
            raise RuntimeError("persistence failure")
        return changed

    monkeypatch.setattr(ReportRepository, "apply_classification", fail_first_write)
    monkeypatch.setattr(
        batch,
        "LLMClassifier",
        lambda _: SimpleNamespace(
            classify=AsyncMock(
                return_value=ClassificationResult(
                    topics=[Topic.FACTOR_MODEL], method="llm"
                )
            )
        ),
    )
    audit = asyncio.run(
        batch.classify_async(batch.ClassificationOptions(all=True, llm=True))
    )
    assert audit["failed_ids"] == [1]
    assert audit["classified_ids"] == [2]
    assert audit["updated"] == 1
    with classification_library() as db:
        assert db.get(Report, 1).topics == "portfolio_optimization"
        assert db.get(Report, 2).topics == "factor_model"


def test_dry_run_can_use_read_only_database(classification_library, monkeypatch):
    path = classification_library.kw["bind"].url.database
    engine = create_engine(f"sqlite:///file:{path}?mode=ro&uri=true")
    readonly = sessionmaker(bind=engine)
    monkeypatch.setattr(batch, "get_session_factory", lambda: readonly)
    monkeypatch.setattr(
        batch,
        "LLMClassifier",
        lambda _: SimpleNamespace(
            classify=AsyncMock(
                return_value=ClassificationResult(
                    topics=[Topic.FACTOR_MODEL], method="llm"
                )
            )
        ),
    )
    try:
        audit = asyncio.run(
            batch.classify_async(
                batch.ClassificationOptions(report_id=1, llm=True, dry_run=True)
            )
        )
        assert audit["classified_ids"] == [1]
        assert audit["updated"] == 1
    finally:
        engine.dispose()
    with classification_library() as db:
        assert db.get(Report, 1).topics == "portfolio_optimization"


def test_dry_run_evaluates_one_guarded_snapshot(classification_library, monkeypatch):
    from reportagent.db.repository import ReportRepository

    result = ClassificationResult(topics=[Topic.FACTOR_MODEL], method="llm")
    scalar = classification_library.class_.scalar
    edited = []

    def race_after_guard(session, statement, **kwargs):
        value = scalar(session, statement, **kwargs)
        if not edited:
            with classification_library() as other:
                row = other.get(Report, 1)
                row.abstract = "newer material"
                ReportRepository(other).apply_classification(row, result)
                other.commit()
            edited.append(True)
        return value

    monkeypatch.setattr(classification_library.class_, "scalar", race_after_guard)
    monkeypatch.setattr(
        batch,
        "LLMClassifier",
        lambda _: SimpleNamespace(classify=AsyncMock(return_value=result)),
    )
    audit = asyncio.run(
        batch.classify_async(
            batch.ClassificationOptions(report_id=1, llm=True, dry_run=True)
        )
    )
    assert edited == [True]
    assert audit["classified_ids"] == [1]
    # This is the preview as of the successful guard, before the newer edit.
    assert audit["updated"] == 1
    with classification_library() as db:
        assert db.get(Report, 1).abstract == "newer material"
