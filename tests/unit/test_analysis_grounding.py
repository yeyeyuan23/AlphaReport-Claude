"""Full coverage and configured summaries at the real agent/model boundary."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from reportagent.agents.analysis_agent import AnalysisAgent
from reportagent.models.schemas import AnalysisResult


class ReadingModel:
    model = "fixture-model"

    def __init__(self):
        self.requests = []
        self.calls = []

    async def chat_json(self, messages, **kwargs):
        self.requests.append(messages)
        self.calls.append({"complete": True})
        if len(self.requests) == 1:
            return {"research_question": "问题", "core_contribution": "贡献"}
        if len(self.requests) == 2:
            return {"analysis_points": [{"analysis": "方法"}]}
        from tests.support.report_fixtures import response

        judgment = response()
        return {
            **{k: judgment[k] for k in ("quality", "confidence", "quality_checks")},
            "strengths": ["优势"],
            "weaknesses": ["条件"],
        }

    async def chat(self, messages, **kwargs):
        self.summary_messages = messages
        self.summary_budget = kwargs["max_tokens"]
        self.calls.append({"complete": True})
        return "保留充分小温度这一条件的总结。"


def test_all_enabled_analysis_phases_read_the_complete_long_paper():
    llm = ReadingModel()
    text = (
        "Opening.\n" + "x" * 114990 + "\nTAIL: only for sufficiently small temperature."
    )
    result = asyncio.run(AnalysisAgent(llm).analyze_report_directly("理论", "", text))
    assert result.summary
    assert result.assessment.quality == "strong"
    assert len(llm.requests) == 3
    for request in llm.requests:
        assert "TAIL: only for sufficiently small temperature." in request[1]["content"]
        assert "C029" in request[1]["content"]


def test_input_over_phase_cap_fails_explicitly_instead_of_truncating():
    llm = ReadingModel()
    agent = AnalysisAgent(llm)
    agent._config["analysis"]["phases"]["metadata"]["max_input_chars"] = 1000
    with pytest.raises(ValueError, match="全文.*上限"):
        asyncio.run(agent.analyze_report_directly("理论", "", "x" * 1001))
    assert not llm.requests


def test_summary_uses_shared_config_budget_and_grounding_for_requested_depth(tmp_path):
    llm = ReadingModel()
    agent = AnalysisAgent(llm)
    agent._config["analysis"]["summary"] = {
        "system_prompt": "共享资源：只根据证据。",
        "user_prompt_template": "{title}\n{context}",
        "profiles": {
            "quick": {"max_tokens": 1700, "instruction": "用一段介绍问题与条件"}
        },
    }
    agent._current_depth = "quick"
    result = asyncio.run(
        agent._synthesize_summary(AnalysisResult(research_question="问题"), "标题")
    )
    assert result
    assert llm.summary_budget == 1700
    assert "共享资源：只根据证据。" in llm.summary_messages[0]["content"]
    assert "不能补造" in llm.summary_messages[0]["content"]
    assert "用一段介绍问题与条件" in llm.summary_messages[1]["content"]


def test_failed_summary_call_propagates_and_cannot_be_saved_as_context_fallback():
    llm = ReadingModel()
    llm.chat = AsyncMock(side_effect=RuntimeError("provider failed"))
    with pytest.raises(RuntimeError, match="provider failed"):
        asyncio.run(
            AnalysisAgent(llm)._synthesize_summary(
                AnalysisResult(research_question="问题"), "标题"
            )
        )
