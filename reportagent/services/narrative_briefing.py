"""Grounded narrative requests, preserving full text and structured conditions."""

import json
from copy import deepcopy

from reportagent.services.text_evidence import text_chunks
from reportagent.skills.registry import skill_resource


def build_briefing_messages(report, analysis: dict) -> list[dict]:
    text = getattr(report, "full_text", "") or ""
    if not getattr(report, "has_full_text", False) or len(text.strip()) < 1000:
        raise ValueError("叙事简报需要可用全文；保留已有结构化展示。")
    analysis = deepcopy(analysis)
    if isinstance(analysis.get("assessment"), dict):
        analysis["assessment"].pop("overall_quality_score", None)
    context = json.dumps(analysis, ensure_ascii=False)
    instructions = skill_resource("analyze-report", "grounding.md").read_text(
        encoding="utf-8"
    )
    prompt = (
        skill_resource("analyze-report", "briefing.md")
        .read_text(encoding="utf-8")
        .format(
            title=report.title,
            abstract=report.abstract or "",
            analysis_context=context,
            full_text=json.dumps(text_chunks(text), ensure_ascii=False),
        )
    )
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": prompt},
    ]
