"""Deterministic paper/model doubles shared by offline regression tests."""

import copy
import importlib
import json

from reportagent.models.schemas import AnalysisResult, Assessment, Methodology

TEXT = "We use a rolling out-of-sample evaluation with transaction costs. " * 30


def response():
    evidence = {
        "claim": "作者使用滚动样本外检验并计入交易成本",
        "quote": "We use a rolling out-of-sample evaluation with transaction costs.",
        "chunk_id": "C001",
    }
    return {
        "research_type": "empirical",
        "research_question": "如何构建投资组合？",
        "method": evidence,
        "findings": [evidence],
        "strengths": [evidence],
        "limitations": ["跨市场适用性未报告"],
        "quality": "strong",
        "confidence": "high",
        "critical_concerns": [],
        "reading_advice": "精读样本外检验部分，复现成本设置。",
    }


class Model:
    model = "fixture-model"
    calls = []

    def __init__(self, data):
        self.data = data
        self.requests = []

    async def chat_json(self, messages, **kwargs):
        self.requests.append(messages)
        return copy.deepcopy(self.data)


def module():
    return importlib.import_module("reportagent.services.report_quality")


def paper(**extra):
    from reportagent.services.paper_scoring import (
        scoring_fingerprint,
        evidence_fingerprint,
    )

    result = {
        "id": 1,
        "source": "arxiv",
        "title": "Portfolio research",
        "full_text": TEXT,
        "has_full_text": True,
        "abstract": "We study portfolio allocation and evaluate a rolling trading policy with transaction costs.",
        "selection_relevance": 8,
        "selection_score": 8,
        "selection_reason": "与组合优化有关",
        **extra,
    }
    if "selection_provenance_json" not in extra:
        result["selection_provenance_json"] = json.dumps(
            {
                "fingerprint": scoring_fingerprint(),
                "status": "scored",
                "evidence_fingerprint": evidence_fingerprint(result),
            }
        )
    return result


def complete_analysis():
    return AnalysisResult(
        research_question="问题",
        core_contribution="贡献",
        summary="总结",
        methodology=Methodology(analysis_points=[{"analysis": "方法"}]),
        assessment=Assessment(strengths=["优势"], weaknesses=["限制"]),
    ).model_dump()
