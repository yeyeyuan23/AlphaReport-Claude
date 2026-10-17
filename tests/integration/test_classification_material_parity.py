"""Automatic import and explicit classification use the same bounded source text."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.classifiers.llm_classifier import LLMClassifier
from reportagent.classifiers.provenance import classification_material
from reportagent.classifiers.rule_classifier import RuleClassifier
from reportagent.db.repository import ReportRepository
from reportagent.models.database import Base, Report
from reportagent.models.schemas import SearchResult, SourceType
from reportagent.services import batch_classification as batch
from reportagent.services.collection import classify_for_storage

TITLE = "A study of financial prediction"
INTRO = "We study stock selection and cross-sectional equity stock return forecasting."
LABEL = "【原文引言；未提取到论文摘要】\n"
FULL_TEXT = (
    TITLE + "\n1 Introduction\n" + INTRO + "\n2 Methodology\nBODY_ONLY_SENTINEL bonds."
)


def source():
    return SearchResult(
        title=TITLE,
        source=SourceType.LOCAL_PDF,
        full_text=FULL_TEXT,
        raw_metadata={"introduction": INTRO},
    )


def model_response():
    return {
        "markets": [],
        "asset_classes": ["stock"],
        "frequencies": [],
        "topics": [],
        "custom_tags": [],
        "confidence": 0.9,
        "evidence": {
            "asset_classes.stock": {
                "source": "material",
                "quote": "stock return forecasting",
                "reason": "The study forecasts equity returns.",
            }
        },
    }


@pytest.fixture
def library(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'classification.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(batch, "init_db", lambda: None)
    monkeypatch.setattr(batch, "get_session_factory", lambda: factory)
    yield factory
    engine.dispose()


def imported(factory, model=None):
    classified = asyncio.run(
        classify_for_storage(
            source(), RuleClassifier(), LLMClassifier(model) if model else None, []
        )
    )
    assert classified is not None
    with factory() as db:
        _, ident = ReportRepository(db).upsert_report(classified)
        assert db.get(Report, ident).asset_classes == "stock"
        return ident


@pytest.mark.parametrize("mode", ["rule", "llm", "hybrid"])
def test_import_and_reclassify_keep_identical_labeled_introduction(
    library, monkeypatch, mode
):
    prompts = []

    async def chat_json(messages):
        prompts.append(messages[-1]["content"])
        return model_response()

    model = SimpleNamespace(
        chat_json=chat_json, _client=SimpleNamespace(close=AsyncMock())
    )
    monkeypatch.setattr(batch, "LLMClient", lambda: model)
    ident = imported(library, model if mode != "rule" else None)
    with library() as db:
        initial = db.get(Report, ident)
        initial_provenance = json.loads(initial.classification_provenance_json or "{}")
    audit = asyncio.run(
        batch.classify_async(
            batch.ClassificationOptions(
                report_id=ident, llm=mode == "llm", hybrid=mode == "hybrid"
            )
        )
    )
    assert audit["failed"] == 0 and audit["classified_ids"] == [ident]
    with library() as db:
        row = db.get(Report, ident)
        assert row.asset_classes == "stock"
        assert classification_material(row) == LABEL + INTRO
        if mode != "rule":
            provenance = json.loads(row.classification_provenance_json)
            assert provenance["input_sha256"] == initial_provenance["input_sha256"]
    if mode != "rule":
        assert len(prompts) == 2
        for prompt in prompts:
            assert LABEL + INTRO in prompt
            assert "BODY_ONLY_SENTINEL" not in prompt


@pytest.mark.parametrize("change", ["full_text", "source"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_introduction_classification_rejects_concurrent_source_change(
    library, monkeypatch, change, dry_run
):
    ident = imported(library)

    async def chat_json(messages):
        with library() as db:
            row = db.get(Report, ident)
            setattr(
                row,
                change,
                FULL_TEXT + "\nRevised body." if change == "full_text" else "arxiv",
            )
            db.commit()
        return model_response()

    model = SimpleNamespace(
        chat_json=chat_json, _client=SimpleNamespace(close=AsyncMock())
    )
    monkeypatch.setattr(batch, "LLMClient", lambda: model)
    audit = asyncio.run(
        batch.classify_async(
            batch.ClassificationOptions(report_id=ident, llm=True, dry_run=dry_run)
        )
    )
    assert audit["failed_ids"] == [ident]
    assert audit["classified_ids"] == [] and audit["updated"] == 0
    with library() as db:
        assert db.get(Report, ident).classification_method == "rule"


@pytest.mark.parametrize("abstract", [None, ""])
def test_missing_introduction_never_falls_back_to_entire_stored_body(abstract):
    row = Report(
        title=TITLE,
        abstract=abstract,
        full_text="BODY_ONLY_SENTINEL stock return forecasting",
    )
    assert classification_material(row) == ""


def test_existing_abstract_remains_the_classification_material():
    row = Report(title=TITLE, abstract="The original abstract.", full_text=FULL_TEXT)
    assert classification_material(row) == "The original abstract."


def test_source_result_without_transient_metadata_recovers_same_introduction():
    paper = source().model_copy(update={"raw_metadata": {}})
    assert classification_material(paper) == LABEL + INTRO
