"""Evidence-grounded reading assessments, shared by daily and single-paper flows."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections import deque
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict

from reportagent.skills.registry import skill_resource
from reportagent.utils.config import get_llm_model, get_llm_base_url, get_llm_provider
from reportagent.services.paper_scoring import scoring_fingerprint

logger = logging.getLogger(__name__)
MAX_REVIEW_CHARS = 60000
SCHEMA_VERSION = 2
TIER_LABELS = {"focus": "重点阅读", "browse": "值得浏览", "pending": "待核验", "exclude": "不推荐"}


class Evidence(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    claim: str = Field(min_length=1)
    quote: str = Field(min_length=20)
    chunk_id: str = Field(pattern=r"^C\d{3,}$")


class Review(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    research_type: Literal["empirical", "theoretical", "methodological", "broker", "community"]
    research_question: str = Field(min_length=1)
    method: Evidence
    findings: list[Evidence] = Field(min_length=1, max_length=5)
    strengths: list[Evidence] = Field(max_length=5)
    limitations: list[str] = Field(min_length=1, max_length=8)
    quality: Literal["strong", "sound", "weak", "unknown"]
    confidence: Literal["high", "medium", "low"]
    critical_concerns: list[str] = Field(max_length=5)
    reading_advice: str = Field(min_length=1)


def source_category(source: str) -> str:
    if source in {"arxiv", "ssrn"}:
        return "academic"
    if source in {"eastmoney", "broker"}:
        return "broker"
    if source == "bigquant":
        return "community"
    return "other"


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def current_relevance(paper: dict) -> float | None:
    """Legacy composite scores cannot qualify as a current direction assessment."""
    try:
        provenance = json.loads(paper.get("selection_provenance_json") or "{}")
    except (ValueError, TypeError):
        return None
    value = paper.get("selection_relevance")
    if (not isinstance(provenance, dict) or provenance.get("fingerprint") != scoring_fingerprint()
            or isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 10):
        return None
    return float(value)


def review_fingerprint(paper: dict, model: str, max_chars: int = MAX_REVIEW_CHARS) -> str:
    payload = {"schema": SCHEMA_VERSION, "model": model, "max_chars": max_chars,
               "endpoint": get_llm_base_url(), "provider": get_llm_provider(),
               "source": paper.get("source"), "revision": paper.get("arxiv_id"),
               "text": paper.get("full_text", ""), "title": paper.get("title"),
               "abstract": paper.get("abstract"), "relevance": paper.get("selection_relevance", paper.get("selection_score")),
               "selection_reason": paper.get("selection_reason"),
               "selection_provenance": paper.get("selection_provenance_json"),
               "current_scoring_rubric": scoring_fingerprint(),
               "prompt": skill_resource("analyze-report", "quality-review.md").read_text(encoding="utf-8")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def pending_review(paper: dict, reason: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "tier": "pending", "label": TIER_LABELS["pending"],
            "status": "pending", "source_category": source_category(paper.get("source", "")),
            "relevance": current_relevance(paper),
            "relevance_reason": paper.get("selection_reason", ""), "quality": "unknown",
            "confidence": "low", "research_question": "", "method": None, "findings": [], "strengths": [],
            "limitations": [reason], "validation_issues": [reason], "critical_concerns": [],
            "reading_advice": "取得并核对原文后再评价研究质量。", "text_complete": False,
            "reviewed_at": datetime.now(timezone.utc).isoformat()}


async def evaluate_paper(paper: dict, llm, *, max_chars: int = MAX_REVIEW_CHARS) -> dict:
    text = paper.get("full_text") or ""
    if not paper.get("has_full_text") or len(text.strip()) < 1000:
        return pending_review(paper, "未取得可用全文；摘要只能用于相关性初筛。")
    content = text[:max_chars]
    chunks = {f"C{i // 4000 + 1:03}": content[i:i + 4000] for i in range(0, len(content), 4000)}
    instructions = skill_resource("analyze-report", "quality-review.md").read_text(encoding="utf-8")
    prompt = json.dumps({"title": paper.get("title"), "source_category": source_category(paper.get("source", "")),
                         "abstract": paper.get("abstract"), "relevance_reason": paper.get("selection_reason"),
                         "text_complete": len(content) == len(text), "text_chunks": chunks}, ensure_ascii=False)
    data = await llm.chat_json([
        {"role": "system", "content": instructions},
        {"role": "user", "content": prompt},
    ], temperature=0.1, max_tokens=6000)
    review = Review.model_validate(data).model_dump()
    issues, rejected = [], []

    def check(item):
        quote = _normalized(item["quote"])
        if quote not in _normalized(chunks.get(item["chunk_id"], "")):
            # Correct a locator only when the exact words exist in another chunk.
            actual = next((key for key, value in chunks.items() if quote in _normalized(value)), None)
            if actual:
                item = {**item, "original_chunk_id": item["chunk_id"], "chunk_id": actual}
            else:
                issues.append(f"引文未能定位：{item['chunk_id']}")
                rejected.append({**item, "verified": False})
                return None
        return {**item, "verified": True, "locator": f"提取文本段 {item['chunk_id']}"}

    review["method"] = check(review["method"])
    for field in ("findings", "strengths"):
        review[field] = [checked for item in review[field] if (checked := check(item))]
    complete = len(content) == len(text)
    if not complete:
        issues.append("全文超过本次输入上限，当前仅完成部分文本评估。")
    relevance = current_relevance(paper)
    if review["quality"] == "unknown":
        issues.append("研究质量尚无法判断，需要补充材料或重新核验。")
    if paper.get("requires_screening_update"):
        issues.append("原文版本或摘要已更新，需要重新进行方向相关性初筛。")
    if issues or not review["method"] or not review["findings"] or review["quality"] == "unknown":
        tier = "pending"
    elif review["quality"] == "weak" or review["critical_concerns"]:
        tier = "exclude"
    elif review["quality"] == "strong" and review["confidence"] == "high" and review["strengths"] and relevance is not None and relevance >= 6:
        tier = "focus"
    else:
        tier = "browse"
    return {**review, "schema_version": SCHEMA_VERSION, "tier": tier, "label": TIER_LABELS[tier],
            "status": "needs_verification" if tier == "pending" else "reviewed", "validation_issues": issues,
            "rejected_evidence": rejected,
            "source_category": source_category(paper.get("source", "")), "text_complete": complete,
            "input_chars": len(content), "full_text_chars": len(text), "relevance": relevance,
            "relevance_reason": paper.get("selection_reason", "") if relevance is not None else "尚未按当前研究方向完成相关性初筛。", "model": llm.model,
            "fingerprint": review_fingerprint(paper, llm.model, max_chars),
            "reviewed_at": datetime.now(timezone.utc).isoformat()}


def select_candidates(papers: list[dict], max_reviews: int = 12, directions: list[str] | None = None) -> list[dict]:
    """Round-robin directions after ranking within them, preserving academic priority."""
    ordered = sorted(papers, key=lambda p: (
        source_category(p.get("source", "")) != "academic",
        -(p.get("selection_relevance") or p.get("selection_score") or 0),
        p.get("published_date") or ""))
    groups: dict[str, deque] = {d: deque() for d in (directions or [])}
    for p in ordered:
        groups.setdefault(p.get("selection_direction") or "other", deque()).append(p)
    selected, seen = [], set()
    while len(selected) < max_reviews and any(groups.values()):
        for queue in groups.values():
            if queue:
                p = queue.popleft()
                if p["id"] not in seen:
                    selected.append(p)
                    seen.add(p["id"])
                    if len(selected) >= max_reviews:
                        break
    return selected


async def review_report_ids(report_ids: list[int], *, force: bool = False) -> dict[int, dict]:
    """Read and save each assessment without holding a database transaction over HTTP."""
    from reportagent.db.engine import get_session_factory
    from reportagent.models.database import Report
    from reportagent.llm.client import LLMClient
    from reportagent.services.one_report import collect_paper

    fields = ("id", "source", "title", "authors", "abstract", "full_text", "has_full_text", "arxiv_id",
              "selection_score", "selection_relevance", "selection_reason", "selection_direction",
              "selection_provenance_json", "source_url", "published_date", "pdf_path")

    def snapshot(row):
        return {key: getattr(row, key) for key in fields}

    factory, results, llm = get_session_factory(), {}, None
    try:
        for rid in report_ids:
            with factory() as session:
                row = session.get(Report, rid)
                if row is None:
                    continue
                paper = snapshot(row)
                raw_cache = row.evaluation_json
            try:
                try:
                    cached = json.loads(raw_cache or "null")
                except (ValueError, TypeError):
                    cached = None
                if not isinstance(cached, dict):
                    cached = None
                if (not paper["has_full_text"] or not paper["full_text"]) and paper["source"] == "arxiv" and paper["arxiv_id"]:
                    fetched = await collect_paper(paper["arxiv_id"])
                    with factory() as session:
                        row = session.get(Report, rid)
                        if row is None or snapshot(row) != paper:
                            results[rid] = pending_review(paper, "获取期间论文或筛选结果已更新，请重新评估。")
                            continue
                        screening_changed = any(getattr(fetched, key) != paper[key]
                                                for key in ("title", "abstract", "arxiv_id"))
                        for key in ("title", "full_text", "abstract", "arxiv_id", "source_url", "pdf_path", "published_date", "tables_json", "equations_json"):
                            setattr(row, key, getattr(fetched, key))
                        row.authors = json.dumps(fetched.authors, ensure_ascii=False)
                        row.has_full_text = bool(fetched.full_text) and not fetched.abstract_only
                        row.analysis_json = row.evaluation_json = row.knowledge_card_json = row.summary = None
                        if screening_changed:
                            row.selection_score = 0.0
                            row.selection_reason = row.selection_relevance = row.selection_direction = row.selection_provenance_json = None
                            row.is_selected = False
                        session.commit()
                        paper = snapshot(row)
                    if screening_changed:
                        paper["requires_screening_update"] = True
                    cached = None
                fingerprint = review_fingerprint(paper, get_llm_model())
                if not force and cached and cached.get("fingerprint") == fingerprint and cached.get("status") == "reviewed" and cached.get("tier") != "pending":
                    results[rid] = cached
                    continue
                if not paper["has_full_text"] or len((paper["full_text"] or "").strip()) < 1000:
                    result = pending_review(paper, "未取得可用全文；暂不作研究质量推荐。")
                else:
                    if llm is None:
                        llm = LLMClient(thinking="disabled")
                    result = await asyncio.wait_for(evaluate_paper(paper, llm), timeout=240)
            except Exception as exc:
                logger.warning("Report %s review failed: %s", rid, type(exc).__name__)
                result = pending_review(paper, f"全文评估未完成（{type(exc).__name__}），可重试。")
            with factory() as session:
                row = session.get(Report, rid)
                if row is None:
                    continue
                # Never attach results to a different evidence or scoring snapshot.
                if snapshot(row) != {key: paper[key] for key in fields}:
                    results[rid] = pending_review(snapshot(row), "分析期间原文或筛选结果已更新，请重新评估。")
                    continue
                row.evaluation_json = json.dumps(result, ensure_ascii=False)
                session.commit()
            results[rid] = result
    finally:
        if llm is not None:
            await llm._client.close()
    return results
