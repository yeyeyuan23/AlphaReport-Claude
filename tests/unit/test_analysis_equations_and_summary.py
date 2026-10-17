"""Shared summary prompts preserve the existing structured analysis context."""

from copy import deepcopy

from reportagent.services.summary import build_summary_request


def _build_summary_from_analysis(analysis, title):
    return build_summary_request(title, analysis=analysis).messages[1]["content"]


def test_analysis_point_summary_preserves_report_title_and_top_level_analysis():
    analysis = {
        "research_question": "原始研究问题",
        "core_contribution": "原始核心贡献",
        "methodology": {
            "analysis_points": [
                {
                    "title": "第一分析条目",
                    "analysis": "第一条目分析",
                    "marginal_contribution": "条目边际贡献",
                    "practical_implication": "条目实践推论",
                    "related_formulas": [0],
                },
                {"title": "第二分析条目", "analysis": "第二条目分析"},
            ],
            "factor_list": [
                {"name": "原始因子", "type": "technical", "construction": "构建"}
            ],
        },
        "equations": [{"latex": "x=y", "meaning": "公式含义"}],
        "assessment": {
            "strengths": ["原始优势"],
            "weaknesses": ["原始局限"],
            "marginal_contribution_summary": "原始贡献总评",
            "practical_implications": ["原始实践推论"],
            "overall_quality_score": 7,
        },
    }
    original = deepcopy(analysis)

    prompt = _build_summary_from_analysis(analysis, "原论文标题")

    for expected in (
        "论文标题：原论文标题",
        "研究问题：原始研究问题",
        "核心贡献：原始核心贡献",
        "**第一分析条目**：第一条目分析",
        "**第二分析条目**：第二条目分析",
        "对应公式 $x=y$（公式含义）",
        "条目边际贡献",
        "条目实践推论",
        "原始因子",
        "原始优势",
        "原始局限",
        "原始贡献总评",
        "原始实践推论",
        "综合评分：7",
    ):
        assert expected in prompt
    assert analysis == original


def test_summary_without_analysis_points_keeps_equation_fallback():
    prompt = _build_summary_from_analysis(
        {"methodology": None, "equations": [{"latex": "x=y", "meaning": "公式含义"}]},
        "原论文标题",
    )

    assert "论文标题：原论文标题" in prompt
    assert "公式：$x=y$ — 公式含义" in prompt
