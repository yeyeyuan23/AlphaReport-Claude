"""One evidence-preserving summary request and execution policy for all entrances."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reportagent.models.schemas import AnalysisResult


def service():
    from reportagent.services import summary

    return summary


@pytest.mark.parametrize(
    "depth,budget", [("quick", 1800), ("standard", 6000), ("deep", 8000)]
)
def test_summary_request_profiles_keep_grounding_and_complete_original_text(
    depth, budget
):
    summary = service()
    text = (
        "opening\n" + "x" * 350000 + "\nTAIL evidence: sufficiently small temperature"
    )
    request = summary.build_summary_request("Title", content=text, depth=depth)
    assert request.max_tokens == budget
    assert "不能补造" in request.messages[0]["content"]
    assert text in request.messages[1]["content"]
    assert "原文材料" in request.messages[1]["content"]
    assert "已完成的分析" not in request.messages[1]["content"]


def test_summary_request_refuses_empty_material():
    summary = service()
    with pytest.raises(ValueError):
        summary.build_summary_request("Title", content=" ")


def test_summary_keeps_full_structured_evidence_including_assumptions_and_benchmarks():
    summary = service()
    data = {
        "research_question": "Question",
        "core_contribution": "Contribution",
        "methodology": {
            "analysis_points": [
                {
                    "title": "Insight",
                    "analysis": "Observed method",
                    "related_formulas": [-1, "bad", 0],
                }
            ],
            "model_architecture": {"description": "Only under assumption A"},
        },
        "equations": [{"latex": "x=y", "meaning": "Equality"}],
        "benchmark_models": ["Matched linear predictor"],
        "limitations": "TAIL limitation",
    }
    request = summary.build_summary_request("Title", analysis=data)
    prompt = request.messages[1]["content"]
    for expected in (
        "Only under assumption A",
        "Matched linear predictor",
        "TAIL limitation",
        "对应公式 $x=y$",
    ):
        assert expected in prompt


def test_summary_empty_execution_never_succeeds():
    summary = service()
    request = summary.build_summary_request(
        "Title", analysis=AnalysisResult(research_question="Question")
    )
    llm = SimpleNamespace(calls=[], chat=AsyncMock(return_value=" "))
    with pytest.raises(ValueError, match="空"):
        asyncio.run(summary.generate_summary(request, llm))


def test_summary_execution_normalizes_latex_and_uses_requested_budget():
    summary = service()
    request = summary.build_summary_request(
        "Title", content="Actual original evidence", depth="quick"
    )
    llm = SimpleNamespace(
        calls=[], chat=AsyncMock(return_value=r"Variable \(x\) is observed.")
    )
    assert (
        asyncio.run(summary.generate_summary(request, llm))
        == "Variable $x$ is observed."
    )
    assert llm.chat.await_args.args[0] == request.messages
    assert llm.chat.await_args.kwargs["max_tokens"] == 1800


def test_summary_rejects_recorded_incomplete_call():
    summary = service()
    request = summary.build_summary_request("Title", content="Actual original evidence")
    llm = SimpleNamespace(calls=[])

    async def complete(*_, **__):
        llm.calls.append({"complete": False})
        return "Partial response"

    llm.chat = complete
    with pytest.raises(ValueError, match="完整"):
        asyncio.run(summary.generate_summary(request, llm))


def test_summary_rejects_empty_analysis_instead_of_generating_from_schema_defaults():
    summary = service()
    with pytest.raises(ValueError, match="空"):
        summary.build_summary_request("Title", analysis=AnalysisResult())


def test_summary_changed_policy_between_request_and_execution_cannot_succeed(
    monkeypatch,
):
    summary = service()
    request = summary.build_summary_request("Title", content="Actual source evidence")
    monkeypatch.setattr(summary, "get_llm_model", lambda: "new-model")
    llm = SimpleNamespace(calls=[], chat=AsyncMock(return_value="A plausible summary"))
    with pytest.raises(ValueError, match="规则已更新"):
        asyncio.run(summary.generate_summary(request, llm))
