"""Saved analysis has one deterministic, lossless briefing presentation."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from reportagent.api import library
from reportagent.services.report_views import map_analysis_to_briefing_sections


@pytest.fixture
def rich_analysis():
    return {
        "research_question": "Does conditional risk improve allocation?",
        "core_contribution": "A conditional portfolio rule",
        "data_used": {
            "market": "US",
            "sample_period": "2000-01-01 to 2025-12-31",
            "frequency": "daily",
            "universe": "S&P 500",
            "instruments": ["SPY", "Treasury futures"],
        },
        "benchmark_models": ["Historical quantile", "Linear QR"],
        "methodology": {
            "analysis_points": [
                {"title": "Validation", "analysis": "Walk forward only"}
            ],
            "factor_list": [
                {
                    "name": "Momentum",
                    "type": "price",
                    "construction": "12m return",
                    "raw_or_neutralized": "sector neutral",
                }
            ],
            "model_architecture": {
                "type": "LSTM",
                "layers_or_structure": "Two recurrent layers",
                "loss_function": r"\rho_\tau(y-q)",
                "regularization": "Dropout 0.2",
                "training_scheme": "Rolling 5y train / 1y test",
            },
            "portfolio_construction": {
                "weighting": "Inverse risk",
                "rebalance_frequency": "Monthly",
                "constraints": "Long only",
                "transaction_cost_model": "10 bps per turnover",
            },
        },
        "equations": [
            {
                "latex": "q_t=f(x_t)",
                "meaning": "Conditional risk forecast",
                "is_key_formula": True,
            },
            {"latex": "x=0", "meaning": "Auxiliary identity", "is_key_formula": False},
        ],
        "assessment": {
            "overall_quality_score": 0.82,
            "strengths": ["Out of sample"],
            "weaknesses": ["One market"],
            "key_contributions": ["Conditional allocation"],
            "marginal_contribution_summary": "Improves the risk target",
            "bias_risks": {
                "look_ahead_bias": "low",
                "survivorship_bias": "medium",
                "data_snooping": "unknown",
                "overfitting_concern": "Repeated tuning",
            },
            "reproducibility": {
                "level": "high",
                "has_data_source": True,
                "has_code_available": True,
            },
            "a_share_applicability": {
                "directly_applicable": False,
                "adaptations_needed": ["T+1 execution"],
                "key_constraints": ["Price limits"],
            },
            "practical_implications": ["Validate trading costs"],
        },
        "summary": "Risk control depends on calibration",
    }


def test_briefing_preserves_rich_saved_methods_formulas_and_assessment(rich_analysis):
    sections = map_analysis_to_briefing_sections(rich_analysis)
    assert [section["title"] for section in sections] == [
        "研究背景与问题",
        "核心方法",
        "关键创新点",
        "研究证据与评估",
        "实践启示",
        "总结",
    ]
    text = "\n".join(section["content"] for section in sections)
    for expected in [
        "2000-01-01 to 2025-12-31",
        "SPY、Treasury futures",
        "Historical quantile、Linear QR",
        "Walk forward only",
        "sector neutral",
        "LSTM",
        "Two recurrent layers",
        r"$\rho_\tau(y-q)$",
        "Dropout 0.2",
        "Rolling 5y train / 1y test",
        "Inverse risk",
        "Monthly",
        "Long only",
        "10 bps per turnover",
        "$$q_t=f(x_t)$$",
        "Conditional risk forecast",
        "前视偏差：低风险",
        "幸存者偏差：中风险",
        "数据窥探：未知",
        "Repeated tuning",
        "high · 有数据源 · 有代码",
        "需要适配",
        "T+1 execution",
        "Price limits",
        "Conditional allocation",
        "Improves the risk target",
        "Validate trading costs",
        "Risk control depends on calibration",
    ]:
        assert expected in text
    assert "8.2 / 10" not in text
    assert "综合评分" not in text
    assert "Auxiliary identity" not in text


def test_instrument_only_analysis_is_not_dropped():
    sections = map_analysis_to_briefing_sections(
        {"data_used": {"instruments": ["SPY"]}}
    )
    assert sections[0]["content"] == "**数据：** 工具：SPY"
    assert map_analysis_to_briefing_sections({}) == []


def test_detail_and_default_endpoint_return_same_canonical_sections_without_model(
    monkeypatch, rich_analysis
):
    report = SimpleNamespace(analysis_json=json.dumps(rich_analysis))
    monkeypatch.setattr(library.ReportRepository, "get_report", lambda *_: report)
    monkeypatch.setattr(
        library.report_reads,
        "get_report_detail",
        lambda *_args, **_kwargs: {"id": 7, "analysis": rich_analysis},
    )
    from reportagent.llm.client import LLMClient

    model = Mock(
        side_effect=AssertionError("Displaying saved analysis must not call a model")
    )
    monkeypatch.setattr(LLMClient, "__init__", model)
    detail = library.get_report(7, db=object())["data"]
    result = asyncio.run(library.get_research_briefing(7, force=False, db=object()))[
        "data"
    ]
    assert detail["briefing_sections"] == result["sections"]
    assert "$$q_t=f(x_t)$$" in detail["briefing_sections"][1]["content"]
    assert result["source"] == "mapped"
    model.assert_not_called()


def test_detail_without_current_analysis_supplies_empty_sections(monkeypatch):
    monkeypatch.setattr(
        library.report_reads, "get_report_detail", lambda *_args, **_kwargs: {"id": 7}
    )
    assert library.get_report(7, db=object())["data"]["briefing_sections"] == []


def test_missing_optional_lists_do_not_hide_the_saved_summary():
    sections = map_analysis_to_briefing_sections(
        {
            "methodology": {"analysis_points": None, "factor_list": None},
            "equations": None,
            "summary": "Saved conclusion",
        }
    )
    assert sections == [{"title": "总结", "icon": "📌", "content": "Saved conclusion"}]
