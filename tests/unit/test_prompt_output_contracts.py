"""Narrative and weekly output contracts cover the bugs found in the prompt audit."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from reportagent.services.weekly_synthesis import _validate_summaries


def test_weekly_rejects_duplicate_paper_conclusions():
    value = {
        "directions": [
            {
                "direction": "risk",
                "updates": [
                    {"text": text, "paper_ids": [1]} for text in ["一段", "另一段"]
                ],
            }
        ]
    }
    with pytest.raises(ValueError):
        _validate_summaries(value, {"risk": {1}})


def test_one_long_mixed_language_conclusion_does_not_discard_other_papers():
    text = "在明确假设下，" + "English method name " * 10 + "给出有条件的理论结论。"
    value = {
        "directions": [
            {
                "direction": "risk",
                "updates": [
                    {"text": text, "paper_ids": [1]},
                    {"text": "第二篇保留自己的结论。", "paper_ids": [2]},
                ],
            }
        ]
    }
    updates = _validate_summaries(value, {"risk": {1, 2}})["risk"]
    assert updates == value["directions"][0]["updates"]


def test_weekly_normalizes_whitespace_without_modifying_model_output():
    value = {
        "directions": [
            {
                "direction": "risk",
                "updates": [{"text": "  第一项。\n\n第二项。 ", "paper_ids": [1]}],
            }
        ]
    }
    before = copy.deepcopy(value)
    assert (
        _validate_summaries(value, {"risk": {1}})["risk"][0]["text"]
        == "第一项。 第二项。"
    )
    assert value == before


def test_forced_narrative_reads_whole_evidence_and_shared_grounding(monkeypatch):
    from reportagent.api import library
    from reportagent.llm.client import LLMClient
    from reportagent.skills.registry import skill_resource

    tail = "TAIL: theorem holds only at sufficiently small temperature."
    method_tail = "METHOD TAIL: conditional on continuous price paths."
    analysis = {
        "research_question": "问题",
        "assessment": {"overall_quality_score": 0.85},
        "methodology": {"analysis_points": [{"analysis": "m" * 700 + method_tail}]},
    }
    report = SimpleNamespace(
        title="理论论文",
        abstract="摘要",
        full_text="x" * 2000 + tail,
        has_full_text=True,
        analysis_json=json.dumps(analysis),
    )
    monkeypatch.setattr(library.ReportRepository, "get_report", lambda *_: report)
    monkeypatch.setattr(LLMClient, "__init__", lambda self: None)
    requests = []

    async def chat(self, messages, **kwargs):
        requests.append(messages)
        return "作者在明确假设下证明结论，未报告交易实验。"

    monkeypatch.setattr(LLMClient, "chat", chat)
    result = asyncio.run(library.get_research_briefing(1, force=True, db=object()))
    assert result["data"]["source"] == "llm"
    assert tail in requests[0][1]["content"]
    assert method_tail in requests[0][1]["content"]
    assert "overall_quality_score" not in requests[0][1]["content"]
    assert analysis["assessment"]["overall_quality_score"] == 0.85
    assert (
        skill_resource("analyze-report", "grounding.md").read_text()
        in requests[0][0]["content"]
    )


def test_long_partial_excerpt_is_not_presented_as_full_text():
    from reportagent.services.narrative_briefing import build_briefing_messages

    report = SimpleNamespace(
        title="网页摘录", abstract="摘要", full_text="摘录" * 1200, has_full_text=False
    )
    with pytest.raises(ValueError, match="可用全文"):
        build_briefing_messages(report, {"research_question": "问题"})
