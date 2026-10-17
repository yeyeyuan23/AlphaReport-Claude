"""Legacy analysis adapters retain source evidence across partial model replies."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from reportagent.agents.analysis_agent import explain_equations
from reportagent.api.reports import _build_summary_from_analysis


def _explain(equations, responses, batch_size=3):
    client = AsyncMock()
    client.chat_json.side_effect = responses
    result = asyncio.run(
        explain_equations(
            equations,
            "原论文标题",
            "原文上下文",
            llm_client=client,
            config={
                "analysis": {
                    "phases": {"equations": {"max_formulas_per_batch": batch_size}}
                }
            },
        )
    )
    return result, client


def test_equation_batches_use_global_indices_and_keep_source_order_and_metadata():
    equations = [
        {"latex": f"x_{index}=y_{index}", "page_idx": index + 10, "bbox": [index]}
        for index in range(7)
    ]
    original = deepcopy(equations)
    responses = [
        {
            "equations": [
                {
                    "index": index,
                    "latex": "model replacement must not be trusted",
                    "page_idx": 999,
                    "meaning": f"含义{index}",
                    "symbols": {f"x_{index}": "原符号"},
                    "role_in_paper": f"作用{index}",
                    "is_key_formula": True,
                }
                for index in indices
            ]
        }
        for indices in ([2, 0, 1], [5, 3, 4], [6])
    ]

    result, client = _explain(equations, responses)

    assert len(result) == len(equations)
    for index, enriched in enumerate(result):
        assert enriched["latex"] == equations[index]["latex"]
        assert enriched["page_idx"] == equations[index]["page_idx"]
        assert enriched["bbox"] == equations[index]["bbox"]
        assert enriched["meaning"] == f"含义{index}"
        assert enriched["symbols"] == {f"x_{index}": "原符号"}
    assert equations == original
    assert (
        "公式 3: x_3=y_3" in client.chat_json.await_args_list[1].args[0][1]["content"]
    )


def test_missing_invalid_and_duplicate_equation_entries_do_not_drop_or_repeat_sources():
    equations = [{"latex": f"x_{index}", "page_idx": index} for index in range(6)]
    result, _ = _explain(
        equations,
        [
            {
                "equations": [
                    {"index": 1, "meaning": "正确解释"},
                    None,
                    "malformed entry",
                    {"meaning": "missing index"},
                    {"index": -1, "meaning": "negative index"},
                    {"index": True, "meaning": "boolean index"},
                    {"index": "0", "meaning": "string index"},
                    {"index": 0.0, "meaning": "float index"},
                    {"index": 5, "meaning": "another batch"},
                    {"index": 99, "meaning": "unknown formula"},
                    {"index": 1, "meaning": "duplicate explanation"},
                ]
            },
            {
                "equations": [
                    {"index": 0, "meaning": "previous batch"},
                    {"index": 4, "meaning": "后续正确解释"},
                ]
            },
        ],
    )

    assert len(result) == len(equations)
    assert result[1]["meaning"] == "正确解释"
    assert result[4]["meaning"] == "后续正确解释"
    for index in (0, 2, 3, 5):
        assert result[index] == equations[index]
    assert [equation["latex"] for equation in result] == [
        equation["latex"] for equation in equations
    ]


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        "not an object",
        {},
        {"equations": None},
        {"equations": {}},
        {"equations": []},
    ],
)
def test_invalid_or_empty_equation_responses_preserve_all_source_equations(response):
    equations = [{"latex": "x=y", "page_idx": 7}, "z=w"]

    result, _ = _explain(equations, [response])

    assert result == [{"latex": "x=y", "page_idx": 7}, {"latex": "z=w"}]


def test_failed_equation_batch_preserves_sources_and_continues_later_batches():
    equations = [{"latex": "x=y", "page_idx": 7}, {"latex": "z=w", "page_idx": 8}]

    result, client = _explain(
        equations,
        [
            RuntimeError("model unavailable"),
            {"equations": [{"index": 1, "meaning": "解释"}]},
        ],
        batch_size=1,
    )

    assert result[0] == equations[0]
    assert result[1]["latex"] == "z=w"
    assert result[1]["page_idx"] == 8
    assert result[1]["meaning"] == "解释"
    assert client.chat_json.await_count == 2


def test_invalid_explanation_fields_do_not_replace_existing_source_metadata():
    equations = [{"latex": "x=y", "page_idx": 7, "meaning": "已有解释"}]

    result, _ = _explain(
        equations,
        [
            {
                "equations": [
                    {
                        "index": 0,
                        "latex": "invented formula",
                        "page_idx": 99,
                        "meaning": None,
                        "symbols": ["not a symbol map"],
                        "role_in_paper": {"not": "text"},
                        "is_key_formula": "false",
                    }
                ]
            }
        ],
    )

    assert result == equations


def test_empty_equation_input_does_not_call_model():
    result, client = _explain([], [])

    assert result == []
    client.chat_json.assert_not_awaited()


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
