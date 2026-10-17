"""A taxonomy value is not evidence that the paper supports that label."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reportagent.classifiers.llm_classifier import LLMClassifier
from reportagent.models.schemas import ClassificationResult, Topic
from reportagent.services.batch_classification import _merge_results


def response():
    return {
        "markets": [],
        "asset_classes": ["stock"],
        "frequencies": [],
        "topics": ["ai_ml_model", "statistical_arbitrage"],
        "custom_tags": ["graph learning", "pairs trading"],
        "confidence": 0.8,
        "evidence": {
            "asset_classes.stock": {
                "source": "title",
                "quote": "Stock prediction",
                "reason": "Equity prediction",
            },
            "topics.ai_ml_model": {
                "source": "material",
                "quote": "graph learning",
                "reason": "Learned model",
            },
            "custom_tags.graph learning": {
                "source": "material",
                "quote": "graph learning",
                "reason": "Explicit method",
            },
            "custom_tags.pairs trading": {
                "source": "material",
                "quote": "We trade cointegrated pairs",
                "reason": "Pairs strategy",
            },
        },
    }


def classify(data, text="We use graph learning with earnings calls."):
    client = SimpleNamespace(chat_json=AsyncMock(return_value=data))
    return asyncio.run(LLMClassifier(client).classify("Stock prediction", text))


def test_missing_or_fabricated_evidence_cannot_add_labels():
    result = classify(response())
    assert result.topics == [Topic.AI_ML_MODEL]
    assert result.custom_tags == ["graph learning"]
    assert set(result.provenance["rejected_labels"]) == {
        "topics.statistical_arbitrage",
        "custom_tags.pairs trading",
    }
    assert (
        result.provenance["labels"]["topics.ai_ml_model"]["quote"] == "graph learning"
    )


def test_pdf_layout_normalization_preserves_verifiable_raw_quote():
    data = response()
    data["evidence"]["topics.ai_ml_model"]["quote"] = "graph learning"
    result = classify(data, "We use graph\nlearning to ﬁnd signals.")
    assert result.topics == [Topic.AI_ML_MODEL]
    assert (
        result.provenance["labels"]["topics.ai_ml_model"]["quote"] == "graph\nlearning"
    )


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        {},
        {
            "topics.ai_ml_model": {
                "source": "material",
                "quote": "Stock prediction",
                "reason": "Wrong source",
            }
        },
    ],
)
def test_no_supported_labels_is_a_failure_not_a_successful_empty_classification(
    evidence,
):
    data = response()
    data.update(
        markets=[],
        asset_classes=[],
        topics=["ai_ml_model"],
        custom_tags=[],
        evidence=evidence,
    )
    with pytest.raises(ValueError, match="evidence"):
        classify(data)


def test_hybrid_cannot_restore_rule_labels_rejected_by_model():
    model = classify(response())
    result = _merge_results(model)
    assert result.topics == [Topic.AI_ML_MODEL]
    assert result.custom_tags == ["graph learning"]
    assert result.provenance == model.provenance


def test_collection_cannot_restore_unverified_rule_labels():
    from reportagent.models.schemas import SearchResult, SourceType
    from reportagent.services.collection import classify_for_storage

    rules = SimpleNamespace(
        classify=lambda *args: ClassificationResult(topics=[Topic.STATISTICAL_ARB])
    )
    model = SimpleNamespace(classify=AsyncMock(return_value=classify(response())))
    paper = SearchResult(
        title="Quantitative stock prediction model",
        abstract="We use graph learning to predict stock returns.",
        source=SourceType.LOCAL_PDF,
    )
    final = asyncio.run(classify_for_storage(paper, rules, model, [])).classification
    assert final.topics == [Topic.AI_ML_MODEL]
    assert final.provenance["labels"]["topics.ai_ml_model"]["quote"] == "graph learning"


@pytest.mark.parametrize("quote", [".", "e", " "])
def test_non_substantive_quotes_do_not_support_a_label(quote):
    data = response()
    data["evidence"]["topics.statistical_arbitrage"] = {
        "source": "material",
        "quote": quote,
        "reason": "Pairs strategy",
    }
    result = classify(data)
    assert Topic.STATISTICAL_ARB not in result.topics


def test_repair_asks_for_exact_pdf_quote_once_and_preserves_raw_evidence():
    first = response()
    first.update(topics=["ai_ml_model"], custom_tags=[])
    first["evidence"]["topics.ai_ml_model"]["quote"] = "MLP-based architecture"
    second = {
        **first,
        "evidence": {
            **first["evidence"],
            "topics.ai_ml_model": {
                "source": "material",
                "quote": "MLP-\nbased architecture",
                "reason": "Neural architecture",
            },
        },
    }
    client = SimpleNamespace(chat_json=AsyncMock(side_effect=[first, second]))
    result = asyncio.run(
        LLMClassifier(client).classify(
            "Stock prediction", "We propose an MLP-\nbased architecture."
        )
    )
    assert result.topics == [Topic.AI_ML_MODEL]
    assert (
        result.provenance["labels"]["topics.ai_ml_model"]["quote"]
        == "MLP-\nbased architecture"
    )
    assert result.provenance["repair_attempted"] is True


def test_failed_quote_repair_retains_only_previously_verified_subset():
    client = SimpleNamespace(
        chat_json=AsyncMock(side_effect=[response(), RuntimeError("timeout")])
    )
    result = asyncio.run(
        LLMClassifier(client).classify("Stock prediction", "We use graph learning.")
    )
    assert result.topics == [Topic.AI_ML_MODEL]
    assert result.custom_tags == ["graph learning"]
    assert result.provenance["repair_status"] == "failed"


def test_all_invalid_model_evidence_cannot_restore_rule_guesses():
    from reportagent.models.schemas import SearchResult, SourceType
    from reportagent.services.collection import classify_for_storage

    data = response()
    data.update(evidence={})
    model = LLMClassifier(SimpleNamespace(chat_json=AsyncMock(return_value=data)))
    rules = SimpleNamespace(
        classify=lambda title, text: ClassificationResult(
            topics=[Topic.STATISTICAL_ARB]
        )
    )
    sr = SearchResult(
        title="Quantitative stock prediction model",
        abstract="We use neural networks to predict stock returns.",
        source=SourceType.LOCAL_PDF,
    )
    errors = []
    result = asyncio.run(classify_for_storage(sr, rules, model, errors))
    assert errors
    assert result.classification.topics == []
    assert result.classification.method == "unverified"
