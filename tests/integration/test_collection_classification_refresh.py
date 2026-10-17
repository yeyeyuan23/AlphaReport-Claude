"""Failed automatic refreshes cannot erase verified same-input classifications."""

import asyncio
import copy
import hashlib
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report, Tag
from reportagent.models.schemas import SearchResult, SourceType
from reportagent.services import batch_classification, collection
from tests.support.collection_scoring import RelevanceModel

TITLE = "Quantitative stock portfolio risk forecasting"
MATERIAL = (
    "We study China stock markets with daily observations using a factor model "
    "and XGBoost to forecast portfolio risk out of sample."
)
LABELS = {
    "markets": ["china"],
    "asset_classes": ["stock"],
    "frequencies": ["low_freq"],
    "topics": ["factor_model"],
    "custom_tags": ["XGBoost"],
}
RESPONSE = {
    **LABELS,
    "confidence": 0.91,
    "evidence": {
        f"{field}.{value}": {
            "source": "material",
            "quote": quote,
            "reason": "The paper explicitly studies this setting or method.",
        }
        for field, value, quote in (
            ("markets", "china", "China"),
            ("asset_classes", "stock", "stock markets"),
            ("frequencies", "low_freq", "daily observations"),
            ("topics", "factor_model", "factor model"),
            ("custom_tags", "XGBoost", "XGBoost"),
        )
    },
}


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    rows = [
        SearchResult(
            title=TITLE,
            abstract=MATERIAL,
            source=SourceType.ARXIV,
            arxiv_id="2610.10001v1",
            abstract_only=True,
        )
    ]
    source = SimpleNamespace(
        source_type=SourceType.ARXIV,
        is_available=lambda: True,
        search=AsyncMock(side_effect=lambda _: list(rows)),
    )

    class Model(RelevanceModel):
        response = RESPONSE
        failure = None

        async def chat_json(self, messages, **kwargs):
            if "## 分类维度与可选值" in messages[-1]["content"]:
                if self.failure is not None:
                    raise self.failure
                return copy.deepcopy(self.response)
            return await super().chat_json(messages, **kwargs)

    model = Model()
    for module in (collection, batch_classification):
        monkeypatch.setattr(module, "init_db", lambda: None)
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(collection, "build_sources", lambda _: [source])
    monkeypatch.setattr(collection, "LLMClient", lambda: model)

    def run():
        return asyncio.run(
            collection.collect_reports(
                collection.CollectionOptions(
                    topics="portfolio",
                    sources=source.source_type.value,
                    no_analysis=True,
                    days=0,
                )
            )
        )

    def read():
        with factory() as session:
            row = session.query(Report).one()
            return {
                "id": row.id,
                "title": row.title,
                "abstract": row.abstract,
                "markets": row.markets,
                "asset_classes": row.asset_classes,
                "frequencies": row.frequencies,
                "topics": row.topics,
                "confidence": row.classification_confidence,
                "method": row.classification_method,
                "provenance": json.loads(row.classification_provenance_json or "{}"),
                "tags": sorted((tag.id, tag.dimension, tag.value) for tag in row.tags),
            }

    yield SimpleNamespace(
        run=run, read=read, factory=factory, rows=rows, source=source, model=model
    )
    engine.dispose()


def fail_evidence(ingest):
    ingest.model.response = {**RESPONSE, "evidence": {}}


def test_identical_reimport_retains_verified_evidence_labels_and_custom_tags(ingest):
    assert ingest.run()["status"] == "ok"
    with ingest.factory() as session:
        row = session.query(Report).one()
        row.tags.append(Tag(dimension="reading", value="saved"))
        session.commit()
    before = ingest.read()
    fail_evidence(ingest)
    receipt = ingest.run()
    after = ingest.read()
    assert after["method"] == "llm"
    assert {k: v for k, v in after.items() if k != "provenance"} == {
        k: v for k, v in before.items() if k != "provenance"
    }
    attempt = after["provenance"].pop("last_failed_attempt")
    assert after["provenance"] == before["provenance"]
    assert (
        attempt["input_sha256"]
        == hashlib.sha256((TITLE + "\n" + MATERIAL).encode()).hexdigest()
    )
    assert attempt["status"] == "failed"
    assert attempt["error_kind"] == "invalid_evidence"
    assert datetime.fromisoformat(attempt["attempted_at"]).tzinfo is not None
    assert receipt["status"] == receipt["source_statuses"][0]["status"] == "partial"
    assert receipt["storage"]["updated"] == 1
    assert receipt["source_statuses"][0]["screening"]["cached_scores"] == 1
    assert "stored unclassified" not in json.dumps(receipt)


def test_repeated_failures_replace_one_audit_and_later_success_replaces_labels(ingest):
    ingest.run()
    before = ingest.read()
    fail_evidence(ingest)
    for _ in range(3):
        assert ingest.run()["status"] == "partial"
        after = ingest.read()
        attempt = after["provenance"].pop("last_failed_attempt")
        assert "last_failed_attempt" not in attempt
        assert after["provenance"] == before["provenance"]
    ingest.model.response = {
        **RESPONSE,
        "markets": [],
        "frequencies": [],
        "custom_tags": [],
        "confidence": 0.7,
    }
    assert ingest.run()["status"] == "ok"
    after = ingest.read()
    assert after["method"] == "llm" and after["confidence"] == 0.7
    assert after["markets"] == after["frequencies"] == ""
    assert {(dim, value) for _, dim, value in after["tags"]} == {
        ("asset_class", "stock"),
        ("topic", "factor_model"),
    }
    assert "last_failed_attempt" not in after["provenance"]


@pytest.mark.parametrize("field", ["title", "abstract"])
def test_changed_classification_input_cannot_reuse_old_labels(ingest, field):
    ingest.run()
    ingest.rows[0] = ingest.rows[0].model_copy(
        update={field: getattr(ingest.rows[0], field) + " Changed research."}
    )
    fail_evidence(ingest)
    assert ingest.run()["status"] == "partial"
    after = ingest.read()
    assert after["method"] == "unverified" and after["tags"] == []
    assert "labels" not in after["provenance"]
    assert after[field] == getattr(ingest.rows[0], field)
    assert (
        after["provenance"]["last_failed_attempt"]["input_sha256"]
        != hashlib.sha256((TITLE + "\n" + MATERIAL).encode()).hexdigest()
    )


def test_labelled_introduction_is_the_exact_classification_input(ingest):
    ingest.source.source_type = SourceType.LOCAL_PDF
    ingest.rows[0] = ingest.rows[0].model_copy(
        update={
            "source": SourceType.LOCAL_PDF,
            "abstract": None,
            "raw_metadata": {"introduction": MATERIAL, "abstract_source": None},
        }
    )
    assert ingest.run()["status"] == "ok"
    before = ingest.read()
    assert before["abstract"] is None
    fingerprint = hashlib.sha256(
        (TITLE + "\n【原文引言；未提取到论文摘要】\n" + MATERIAL).encode()
    ).hexdigest()
    assert before["provenance"]["input_sha256"] == fingerprint
    fail_evidence(ingest)
    assert ingest.run()["status"] == "partial"
    after = ingest.read()
    assert after["method"] == "llm" and after["tags"] == before["tags"]
    assert after["provenance"]["last_failed_attempt"]["input_sha256"] == fingerprint
    ingest.rows[0].raw_metadata["introduction"] += " Different introduction."
    assert ingest.run()["status"] == "partial"
    after = ingest.read()
    assert after["method"] == "unverified" and after["tags"] == []
    assert after["abstract"] is None


def test_new_paper_failed_evidence_stays_unverified(ingest):
    fail_evidence(ingest)
    assert ingest.run()["status"] == "partial"
    row = ingest.read()
    assert row["method"] == "unverified" and row["tags"] == []
    assert row["provenance"]["last_failed_attempt"]["error_kind"] == "invalid_evidence"


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "invalid_json",
        "list",
        "no_fingerprint",
        "unknown_schema",
        "bool_schema",
        "empty_labels",
        "malformed_label",
        "wrong_quote",
        "missing_label",
        "missing_tag",
        "unverified_method",
    ],
)
def test_legacy_or_malformed_provenance_cannot_prove_reusable_success(ingest, damage):
    ingest.run()
    with ingest.factory() as session:
        row = session.query(Report).one()
        provenance = json.loads(row.classification_provenance_json)
        if damage == "no_fingerprint":
            provenance.pop("input_sha256")
        elif damage == "unknown_schema":
            provenance["schema_version"] = 99
        elif damage == "bool_schema":
            provenance["schema_version"] = True
        elif damage == "empty_labels":
            provenance["labels"] = {}
        elif damage == "malformed_label":
            provenance["labels"]["custom_tags.XGBoost"] = "not evidence"
        elif damage == "wrong_quote":
            provenance["labels"]["custom_tags.XGBoost"]["quote"] = "not in source"
        elif damage == "missing_label":
            provenance["labels"].pop("custom_tags.XGBoost")
        elif damage == "missing_tag":
            row.tags = [tag for tag in row.tags if tag.dimension != "custom"]
        elif damage == "unverified_method":
            row.classification_method = "unverified"
        row.classification_provenance_json = {
            "missing": None,
            "invalid_json": "{broken",
            "list": "[]",
        }.get(damage, json.dumps(provenance))
        session.commit()
    fail_evidence(ingest)
    assert ingest.run()["status"] == "partial"
    row = ingest.read()
    assert row["method"] == "unverified" and row["tags"] == []


def test_transient_model_failure_retains_success_without_storing_raw_error(
    ingest, caplog
):
    ingest.run()
    before = ingest.read()
    ingest.model.failure = RuntimeError("token=do-not-persist upstream failure")
    receipt = ingest.run()
    row = ingest.read()
    assert row["method"] == "llm" and row["tags"] == before["tags"]
    assert row["confidence"] == before["confidence"]
    assert row["provenance"]["last_failed_attempt"]["error_kind"] == "model_error"
    assert "do-not-persist" not in json.dumps(row) + json.dumps(receipt)
    assert receipt["status"] == "partial"
    assert "rule fallback used" not in json.dumps(receipt)
    assert "do-not-persist" not in caplog.text


def test_explicit_rule_reclassification_still_replaces_verified_labels(ingest):
    ingest.run()
    audit = asyncio.run(
        batch_classification.classify_async(
            batch_classification.ClassificationOptions(report_id=1)
        )
    )
    assert audit["failed"] == 0 and audit["updated"] == 1
    assert ingest.read()["method"] == "rule"
    assert ingest.read()["provenance"] == {}


def test_model_initialization_failure_retains_same_input_with_partial_receipt(
    ingest, monkeypatch
):
    ingest.run()
    before = ingest.read()
    monkeypatch.setattr(
        collection,
        "LLMClient",
        lambda: (_ for _ in ()).throw(RuntimeError("token=do-not-persist unavailable")),
    )
    receipt = ingest.run()
    after = ingest.read()
    assert after["method"] == "llm" and after["tags"] == before["tags"]
    assert after["provenance"]["last_failed_attempt"]["error_kind"] == "model_error"
    assert receipt["status"] == "partial"
    assert len(receipt["source_statuses"][0]["errors"]) == 1
    assert "do-not-persist" not in json.dumps(after) + json.dumps(receipt)


def test_startup_model_unavailable_uses_same_guard_without_duplicate_errors(
    ingest, monkeypatch, tmp_path
):
    from reportagent.services import local_pdf_import

    ingest.source.source_type = SourceType.LOCAL_PDF
    ingest.rows[0].source = SourceType.LOCAL_PDF
    ingest.run()
    before = ingest.read()
    root = tmp_path / "pdf-library"
    root.mkdir()
    (root / "paper.pdf").write_bytes(b"offline input; parser boundary is injected")
    monkeypatch.setattr(local_pdf_import, "get_session_factory", lambda: ingest.factory)
    monkeypatch.setattr(
        local_pdf_import,
        "LocalPDFSource",
        lambda _: SimpleNamespace(_process_pdf=lambda *args: ingest.rows[0]),
    )
    monkeypatch.setattr(
        local_pdf_import,
        "LLMClient",
        lambda: (_ for _ in ()).throw(RuntimeError("token=do-not-persist unavailable")),
    )
    result = dict(
        scanned=0,
        unchanged=0,
        added=0,
        updated=0,
        filtered=0,
        failed=0,
        errors=[],
        report_ids=[],
    )
    asyncio.run(local_pdf_import._scan(root, result))
    after = ingest.read()
    assert after["method"] == "llm" and after["tags"] == before["tags"]
    assert after["provenance"]["last_failed_attempt"]["error_kind"] == "model_error"
    assert result["failed"] == 1 and result["updated"] == 1
    assert len(result["errors"]) == 1
    assert "do-not-persist" not in json.dumps(after) + json.dumps(result)


def test_same_intro_input_reuses_evidence_when_cross_source_keeps_older_abstract(
    ingest,
):
    assert ingest.run()["status"] == "ok"
    ingest.source.source_type = SourceType.LOCAL_PDF
    introduction = MATERIAL + " Introduction context."
    ingest.rows[0] = ingest.rows[0].model_copy(
        update={
            "source": SourceType.LOCAL_PDF,
            "abstract": None,
            "raw_metadata": {"introduction": introduction, "abstract_source": None},
        }
    )
    assert ingest.run()["status"] == "ok"
    before = ingest.read()
    # The repository's independent material policy keeps the online abstract,
    # but the last accepted classification used the supplied labelled introduction.
    assert before["abstract"] == MATERIAL
    assert (
        before["provenance"]["input_sha256"]
        == hashlib.sha256(
            (TITLE + "\n【原文引言；未提取到论文摘要】\n" + introduction).encode()
        ).hexdigest()
    )
    fail_evidence(ingest)
    assert ingest.run()["status"] == "partial"
    after = ingest.read()
    assert after["method"] == "llm" and after["tags"] == before["tags"]
    after["provenance"].pop("last_failed_attempt")
    assert after == before
