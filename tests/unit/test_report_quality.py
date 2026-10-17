import asyncio
from unittest.mock import patch

import pytest


from tests.support.report_fixtures import Model, TEXT, module, paper, response


def test_missing_full_text_never_calls_model_or_becomes_focus():
    llm = Model(response())
    result = asyncio.run(
        module().evaluate_paper(paper(full_text="", has_full_text=False), llm)
    )
    assert result["tier"] == "pending"
    assert not llm.requests


def test_reading_assessment_does_not_require_quotes_or_text_locators():
    data = response()
    data["method"] = {"claim": "作者使用滚动样本外检验并计入交易成本"}
    data["findings"] = [{"claim": "作者报告了样本外结果及适用条件"}]
    data["strengths"] = [{"claim": "明确讨论了方法的适用范围"}]
    result = asyncio.run(module().evaluate_paper(paper(), Model(data)))
    assert result["tier"] == "focus"
    assert result["status"] == "reviewed"
    assert result["findings"] == [{"claim": "作者报告了样本外结果及适用条件"}]
    assert not result["validation_issues"]
    assert result["text_complete"] is True


def test_unlocatable_quote_does_not_override_research_assessment():
    data = response()
    data["findings"] = [
        {
            "claim": "作者讨论了研究边界",
            "quote": "A paraphrase with PDF typesetting differences.",
            "chunk_id": "C009",
        }
    ]
    result = asyncio.run(module().evaluate_paper(paper(), Model(data)))
    assert result["tier"] == "focus"
    assert result["status"] == "reviewed"
    assert result["findings"][0]["claim"] == "作者讨论了研究边界"
    assert result["findings"][0]["quote"] == data["findings"][0]["quote"]
    assert not result["findings"][0].get("verified")
    assert not result["validation_issues"]


def test_partial_input_cannot_be_described_as_completed_full_text_review():
    result = asyncio.run(
        module().evaluate_paper(paper(), Model(response()), max_chars=1000)
    )
    assert result["tier"] == "pending"
    assert result["text_complete"] is False


def test_weak_method_is_not_promoted_by_relevance():
    data = response()
    data.update(quality="weak", critical_concerns=["作者报告的检验设计不足以支持结论"])
    result = asyncio.run(
        module().evaluate_paper(paper(selection_relevance=10), Model(data))
    )
    assert result["tier"] == "exclude"


@pytest.mark.parametrize(
    ("quality", "confidence"),
    [
        ("sound", "medium"),
        ("sound", "high"),
        ("strong", "medium"),
    ],
)
def test_readworthy_relevant_work_can_be_featured_with_disclosed_limitations(
    quality, confidence
):
    data = response()
    data.update(
        quality=quality,
        confidence=confidence,
        strengths=[],
        limitations=["原文明示：目前仅在一个市场检验，跨市场适用性待验证。"],
    )
    result = asyncio.run(
        module().evaluate_paper(paper(selection_relevance=6), Model(data))
    )
    assert result["tier"] == "focus"
    assert result["limitations"] == [
        "原文明示：目前仅在一个市场检验，跨市场适用性待验证。"
    ]
    assert result["quality"] == quality
    assert result["confidence"] == confidence


def test_low_confidence_remains_browsable_without_becoming_a_top_recommendation():
    data = response()
    data["confidence"] = "low"
    result = asyncio.run(
        module().evaluate_paper(paper(selection_relevance=9), Model(data))
    )
    assert result["tier"] == "browse"
    assert result["status"] == "reviewed"


def test_low_relevance_still_does_not_become_a_top_recommendation():
    result = asyncio.run(
        module().evaluate_paper(paper(selection_relevance=5), Model(response()))
    )
    assert result["tier"] == "browse"


def test_explicit_critical_flaw_still_prevents_a_top_recommendation():
    data = response()
    data["critical_concerns"] = ["实验明确将测试集标签用于训练，主要样本外结论不成立。"]
    result = asyncio.run(
        module().evaluate_paper(paper(selection_relevance=9), Model(data))
    )
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
    rows = [
        paper(
            id=i,
            selection_direction="a" if i < 15 else "b",
            selection_relevance=9 if i < 15 else 7,
        )
        for i in range(20)
    ]
    selected = module().select_candidates(rows, max_reviews=12, directions=["a", "b"])
    assert len(selected) == 12
    assert len({p["id"] for p in selected}) == 12
    assert {p["selection_direction"] for p in selected[:2]} == {"a", "b"}


def test_invalid_model_schema_is_failure_not_a_default_positive_review():
    with pytest.raises(ValueError):
        asyncio.run(module().evaluate_paper(paper(), Model({"quality": "strong"})))


def test_whitespace_is_not_a_method_description():
    data = response()
    data["method"]["claim"] = " " * 30
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
    result = asyncio.run(
        module().evaluate_paper(
            paper(selection_provenance_json=provenance), Model(response())
        )
    )
    assert result["tier"] == "browse"
    assert result["relevance"] is None


def test_quote_metadata_is_not_a_requirement_of_reading_assessment():
    data = response()
    data["method"]["chunk_id"] = "unknown PDF page"
    data["method"]["quote"] = ""
    result = asyncio.run(module().evaluate_paper(paper(), Model(data)))
    assert result["tier"] == "focus"
    assert result["method"]["claim"] == data["method"]["claim"]
    assert not result["validation_issues"]


@pytest.mark.parametrize("size", [63000, 76000, 115000])
def test_default_review_includes_long_paper_tail_and_complete_raw_coverage(size):
    text = (
        TEXT
        + "x" * (size - len(TEXT) - 50)
        + "Tail: convexity requires sufficiently small temperature."
    )
    llm = Model(response())
    result = asyncio.run(module().evaluate_paper(paper(full_text=text), llm))
    payload = __import__("json").loads(llm.requests[0][1]["content"])
    assert "".join(payload["text_chunks"].values()) == text
    assert result["input_chars"] == len(text)
    assert result["text_complete"] is True


def test_explicit_review_limit_is_pending_and_never_sends_partial_text():
    llm = Model(response())
    result = asyncio.run(module().evaluate_paper(paper(), llm, max_chars=1000))
    assert result["tier"] == "pending"
    assert not llm.requests


@pytest.mark.parametrize("field", ["title", "abstract"])
def test_changed_material_cannot_use_old_relevance_to_become_focus(field):
    candidate = paper()
    candidate[field] += " New evidence added after relevance screening."
    result = asyncio.run(module().evaluate_paper(candidate, Model(response())))
    assert result["tier"] != "focus"
    assert result["relevance"] is None


def test_pending_status_cannot_use_leftover_numeric_score_to_become_focus():
    import json

    candidate = paper()
    provenance = json.loads(candidate["selection_provenance_json"])
    provenance["status"] = "pending_needs_material"
    candidate["selection_provenance_json"] = json.dumps(provenance)
    result = asyncio.run(module().evaluate_paper(candidate, Model(response())))
    assert result["tier"] != "focus"
    assert result["relevance"] is None


def test_full_text_change_keeps_abstract_relevance_but_invalidates_full_text_caches():
    from types import SimpleNamespace
    from reportagent.services.paper_scoring import screening_is_current
    from reportagent.services.batch_analysis import analysis_cache_fingerprint

    candidate = paper()
    before_review = module().review_fingerprint(candidate, "fixture-model")
    before_analysis = analysis_cache_fingerprint(
        SimpleNamespace(**candidate), "standard"
    )
    candidate["full_text"] += " New evidence added after abstract relevance screening."
    assert screening_is_current(candidate)
    assert module().current_relevance(candidate) == 8
    assert module().review_fingerprint(candidate, "fixture-model") != before_review
    assert (
        analysis_cache_fingerprint(SimpleNamespace(**candidate), "standard")
        != before_analysis
    )
    result = asyncio.run(module().evaluate_paper(candidate, Model(response())))
    assert result["tier"] == "focus"
    assert result["relevance"] == 8


def test_review_fingerprint_records_prompt_actually_sent_during_hot_edit(
    tmp_path, monkeypatch
):
    quality = module()
    resource = tmp_path / "quality-review.md"
    resource.write_text("Original review policy")
    monkeypatch.setattr(quality, "skill_resource", lambda *args: resource)
    p = paper()

    class EditedModel(Model):
        async def chat_json(self, messages, **kwargs):
            resource.write_text("Changed while response in flight")
            return await super().chat_json(messages, **kwargs)

    llm = EditedModel(response())
    original = quality.review_fingerprint(p, llm.model)
    result = asyncio.run(quality.evaluate_paper(p, llm))
    assert result["fingerprint"] == original
    assert result["fingerprint"] != quality.review_fingerprint(p, llm.model)
