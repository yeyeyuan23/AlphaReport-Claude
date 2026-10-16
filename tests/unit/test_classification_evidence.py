"""A taxonomy value is not evidence that the paper supports that label."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reportagent.classifiers.llm_classifier import LLMClassifier
from reportagent.models.schemas import ClassificationResult, Topic
from reportagent.services.batch_classification import _merge_results


def response():
    return {"markets": [], "asset_classes": ["stock"], "frequencies": [],
            "topics": ["ai_ml_model", "statistical_arbitrage"],
            "custom_tags": ["graph learning", "pairs trading"], "confidence": .8,
            "evidence": {
                "asset_classes.stock": {"source": "title", "quote": "Stock prediction", "reason": "Equity prediction"},
                "topics.ai_ml_model": {"source": "material", "quote": "graph learning", "reason": "Learned model"},
                "custom_tags.graph learning": {"source": "material", "quote": "graph learning", "reason": "Explicit method"},
                "custom_tags.pairs trading": {"source": "material", "quote": "We trade cointegrated pairs", "reason": "Pairs strategy"},
            }}


def classify(data, text="We use graph learning with earnings calls."):
    client = SimpleNamespace(chat_json=AsyncMock(return_value=data))
    return asyncio.run(LLMClassifier(client).classify("Stock prediction", text))


def test_missing_or_fabricated_evidence_cannot_add_labels():
    result = classify(response())
    assert result.topics == [Topic.AI_ML_MODEL]
    assert result.custom_tags == ["graph learning"]
    assert set(result.provenance["rejected_labels"]) == {
        "topics.statistical_arbitrage", "custom_tags.pairs trading"}
    assert result.provenance["labels"]["topics.ai_ml_model"]["quote"] == "graph learning"


def test_pdf_layout_normalization_preserves_verifiable_raw_quote():
    data = response()
    data["evidence"]["topics.ai_ml_model"]["quote"] = "graph learning"
    result = classify(data, "We use graph\nlearning to ﬁnd signals.")
    assert result.topics == [Topic.AI_ML_MODEL]
    assert result.provenance["labels"]["topics.ai_ml_model"]["quote"] == "graph\nlearning"


@pytest.mark.parametrize("evidence", [None, {}, {"topics.ai_ml_model": {
    "source": "material", "quote": "Stock prediction", "reason": "Wrong source"}}])
def test_no_supported_labels_is_a_failure_not_a_successful_empty_classification(evidence):
    data = response()
    data.update(markets=[], asset_classes=[], topics=["ai_ml_model"], custom_tags=[], evidence=evidence)
    with pytest.raises(ValueError, match="evidence"):
        classify(data)


def test_hybrid_cannot_restore_rule_labels_rejected_by_model():
    rule = ClassificationResult(topics=[Topic.STATISTICAL_ARB], custom_tags=["pairs trading"])
    model = classify(response())
    result = _merge_results(rule, model)
    assert result.topics == [Topic.AI_ML_MODEL]
    assert result.custom_tags == ["graph learning"]
    assert result.provenance == model.provenance


def test_agent_hybrid_cannot_restore_unverified_rule_labels():
    from reportagent.agents.classification_agent import ClassificationAgent
    from reportagent.models.schemas import SearchResult, SourceType
    rules = SimpleNamespace(classify=lambda title, text: ClassificationResult(topics=[Topic.STATISTICAL_ARB]))
    model = SimpleNamespace(classify=AsyncMock(return_value=classify(response())))
    agent = ClassificationAgent(rules, model)
    state = {"raw_results": [SearchResult(title="Stock prediction", source=SourceType.LOCAL_PDF)], "messages": []}
    final = asyncio.run(agent.run(state))["classified_reports"][0].classification
    assert final.topics == [Topic.AI_ML_MODEL]
    assert final.provenance["labels"]["topics.ai_ml_model"]["quote"] == "graph learning"
