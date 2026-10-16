"""Deterministic, versioned cards projected from current, located paper evidence."""
from __future__ import annotations

import hashlib
import json

from reportagent.services.report_quality import Review, review_fingerprint
from reportagent.services.text_evidence import locate_quote
from reportagent.skills.registry import skill_resource
from reportagent.utils.config import get_llm_model, get_llm_base_url, get_llm_provider


def report_snapshot(report) -> dict:
    if isinstance(report, dict):
        return dict(report)
    fields = ("id", "title", "abstract", "full_text", "has_full_text", "arxiv_id", "source",
              "topics", "asset_classes", "analysis_json", "evaluation_json", "selection_relevance",
              "selection_reason", "selection_provenance_json", "knowledge_card_json")
    return {key: getattr(report, key, None) for key in fields}


def current_review(paper: dict) -> dict | None:
    """Recheck provenance and exact excerpts, including legacy/tampered cache data."""
    try:
        review = json.loads(paper.get("evaluation_json") or "null")
        if not isinstance(review, dict) or not paper.get("has_full_text"):
            return None
        if (review.get("status") != "reviewed" or not review.get("text_complete")
                or review.get("model") != get_llm_model()
                or review.get("fingerprint") != review_fingerprint(paper, get_llm_model())):
            return None
        Review.model_validate(review)
        text = paper.get("full_text") or ""
        for item in [review["method"], *review["findings"], *review["strengths"]]:
            located = locate_quote(text, item["quote"], item["chunk_id"])
            if not item.get("verified") or located is None:
                return None
            item.update(located)
        return review
    except (ValueError, TypeError, KeyError):
        return None


def build_knowledge_card(report) -> dict:
    paper = report_snapshot(report)
    policy = skill_resource("analyze-report", "knowledge-card.md").read_text(encoding="utf-8")
    payload = {"schema": 1, "paper": {k: v for k, v in paper.items() if k != "knowledge_card_json"},
               "policy": policy, "model": get_llm_model(), "provider": get_llm_provider(),
               "endpoint": get_llm_base_url()}
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    card = {"summary": paper.get("abstract") or "", "highlights": [], "methodology_steps": [],
            "results": "", "marginal_contributions": "", "implications": [], "quality_score": None,
            "topics": (paper.get("topics") or "").split(",") if paper.get("topics") else [],
            "asset_classes": (paper.get("asset_classes") or "").split(",") if paper.get("asset_classes") else [],
            "status": "pending", "quality_label": "待核验", "evidence": [], "limitations": [],
            "notice": "仅展示原始摘要；需完成当前原文版本的证据评估后再生成阅读卡片。",
            "provenance": {"fingerprint": fingerprint, "model": get_llm_model(), "schema": 1}}
    review = current_review(paper)
    if review is None:
        return card
    card.update(
        status="ready", quality_label=review.get("label", "阅读评估"),
        summary=review["research_question"],
        highlights=[e["claim"] for e in review["findings"]],
        methodology_steps=[review["method"]["claim"]],
        results="\n\n".join(e["claim"] for e in review["findings"]),
        marginal_contributions="\n".join(e["claim"] for e in review["strengths"]),
        implications=[review["reading_advice"]], limitations=review["limitations"],
        evidence=[review["method"], *review["findings"], *review["strengths"]],
        notice="作者报告的研究结果与条件，来自已保存评估；原文引文可定位，尚未复现实验。",
    )
    return card


def get_knowledge_card(report, *, force: bool = False) -> dict:
    """Prepare cheaply; only successful current cards may populate the shared cache."""
    card = build_knowledge_card(report)
    # Projection is deterministic: do not trust a matching hash on altered content.
    try:
        cached = json.loads(getattr(report, "knowledge_card_json", None) or "null")
    except (ValueError, TypeError):
        cached = None
    if not force and cached == card:
        return cached
    if card["status"] == "ready":
        report.knowledge_card_json = json.dumps(card, ensure_ascii=False)
    return card
