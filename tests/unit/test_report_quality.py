import asyncio
from unittest.mock import patch

import pytest


from tests.support.report_fixtures import Model, TEXT, module, paper, response


def test_missing_full_text_never_calls_model_or_becomes_focus():
    llm = Model(response())
    result = asyncio.run(module().evaluate_paper(paper(full_text="", has_full_text=False), llm))
    assert result["tier"] == "pending"
    assert not llm.requests


def test_review_requires_findable_evidence_and_preserves_its_locator():
    result = asyncio.run(module().evaluate_paper(paper(), Model(response())))
    assert result["tier"] == "focus"
    assert result["findings"][0]["chunk_id"] == "C001"
    assert result["findings"][0]["verified"] is True
    assert result["text_complete"] is True


def test_fabricated_quote_cannot_produce_high_quality_recommendation():
    data = response()
    data["findings"] = [{"claim": "收益提升50%", "quote": "A fabricated performance result of fifty percent.", "chunk_id": "C001"}]
    result = asyncio.run(module().evaluate_paper(paper(), Model(data)))
    assert result["tier"] == "pending"
    assert result["findings"] == []
    assert result["validation_issues"]


def test_partial_input_cannot_be_described_as_completed_full_text_review():
    result = asyncio.run(module().evaluate_paper(paper(), Model(response()), max_chars=1000))
    assert result["tier"] == "pending"
    assert result["text_complete"] is False


def test_weak_method_is_not_promoted_by_relevance():
    data = response()
    data.update(quality="weak", critical_concerns=["作者报告的检验设计不足以支持结论"])
    result = asyncio.run(module().evaluate_paper(paper(selection_relevance=10), Model(data)))
    assert result["tier"] == "exclude"


def test_theoretical_paper_does_not_require_trading_backtest_fields():
    data = response()
    data["research_type"] = "theoretical"
    result = asyncio.run(module().evaluate_paper(paper(), Model(data)))
    assert result["tier"] == "focus"
    assert result["research_type"] == "theoretical"


def test_prompt_change_changes_review_cache_identity(tmp_path):
    prompt = tmp_path / "quality.md"
    prompt.write_text("Old instructions")
    with patch.object(module(), "skill_resource", return_value=prompt):
        before = module().review_fingerprint(paper(), "fixture-model")
        prompt.write_text("New instructions")
        after = module().review_fingerprint(paper(), "fixture-model")
    assert before != after


def test_round_robin_candidate_budget_never_duplicates_or_exceeds_twelve():
    rows = [paper(id=i, selection_direction="a" if i < 15 else "b", selection_relevance=9 if i < 15 else 7) for i in range(20)]
    selected = module().select_candidates(rows, max_reviews=12, directions=["a", "b"])
    assert len(selected) == 12
    assert len({p["id"] for p in selected}) == 12
    assert {p["selection_direction"] for p in selected[:2]} == {"a", "b"}


def test_invalid_model_schema_is_failure_not_a_default_positive_review():
    with pytest.raises(ValueError):
        asyncio.run(module().evaluate_paper(paper(), Model({"quality": "strong"})))


def test_whitespace_is_not_evidence_or_a_research_question():
    data = response()
    data["method"]["quote"] = " " * 30
    with pytest.raises(ValueError):
        asyncio.run(module().evaluate_paper(paper(), Model(data)))


def test_unknown_quality_is_retryable_even_when_quotes_match():
    data = response()
    data["quality"] = "unknown"
    result = asyncio.run(module().evaluate_paper(paper(), Model(data)))
    assert result["tier"] == "pending"
    assert result["status"] != "reviewed"


@pytest.mark.parametrize("provenance", [None, '{"fingerprint":"old"}', "[]", "{broken"])
def test_legacy_or_stale_relevance_cannot_promote_explicit_paper(provenance):
    result = asyncio.run(module().evaluate_paper(paper(selection_provenance_json=provenance), Model(response())))
    assert result["tier"] == "browse"
    assert result["relevance"] is None


def test_exact_quote_in_another_chunk_is_relocated_without_fuzzy_matching():
    data = response()
    data["method"]["chunk_id"] = "C009"
    result = asyncio.run(module().evaluate_paper(paper(), Model(data)))
    assert result["tier"] == "focus"
    assert result["method"]["chunk_id"] == "C001"
    assert result["method"]["original_chunk_id"] == "C009"


def test_rejected_evidence_is_retained_for_audit_but_not_shown_as_a_finding():
    data = response()
    data["findings"][0] = {"claim": "假发现", "quote": "This exact quote is not in this paper.", "chunk_id": "C001"}
    result = asyncio.run(module().evaluate_paper(paper(), Model(data)))
    assert result["findings"] == []
    assert result["rejected_evidence"][0]["quote"] == data["findings"][0]["quote"]
