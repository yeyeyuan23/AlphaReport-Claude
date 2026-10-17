"""Full-text reading assessments, shared by daily and single-paper flows."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections import deque
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict

from reportagent.skills.registry import skill_resource
from reportagent.utils.config import get_llm_model, get_llm_base_url, get_llm_provider
from reportagent.services.paper_scoring import scoring_fingerprint, screening_is_current
from reportagent.services.text_evidence import text_chunks

logger = logging.getLogger(__name__)
MAX_REVIEW_CHARS = 240000
SCHEMA_VERSION = 5
TIER_LABELS = {
    "focus": "重点阅读",
    "browse": "值得浏览",
    "pending": "待核验",
    "exclude": "不推荐",
}


class Evidence(BaseModel):
    """A reading observation with optional references for the reader to consult."""

    model_config = ConfigDict(str_strip_whitespace=True)
    claim: str = Field(min_length=1)
    quote: str | None = None
    chunk_id: str | None = None


class Review(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    research_type: Literal[
        "empirical", "theoretical", "methodological", "broker", "community"
    ]
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


def current_relevance(paper: dict) -> float | None:
    """Legacy composite scores cannot qualify as a current direction assessment."""
    try:
        provenance = json.loads(paper.get("selection_provenance_json") or "{}")
    except (ValueError, TypeError):
        return None
    value = paper.get("selection_relevance")
    if (
        not screening_is_current(paper)
        or provenance.get("status") != "scored"
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 10
    ):
        return None
    return float(value)


def review_fingerprint(
    paper: dict,
    model: str,
    max_chars: int = MAX_REVIEW_CHARS,
    *,
    instructions: str | None = None,
) -> str:
    relevance = paper.get("selection_relevance", paper.get("selection_score"))
    if isinstance(relevance, (int, float)) and not isinstance(relevance, bool):
        relevance = float(
            relevance
        )  # SQLite REAL and JSON integers represent the same score.
    payload = {
        "schema": SCHEMA_VERSION,
        "model": model,
        "max_chars": max_chars,
        "endpoint": get_llm_base_url(),
        "provider": get_llm_provider(),
        "source": paper.get("source"),
        "revision": paper.get("arxiv_id"),
        "text": paper.get("full_text", ""),
        "title": paper.get("title"),
        "abstract": paper.get("abstract"),
        "relevance": relevance,
        "selection_reason": paper.get("selection_reason"),
        "selection_provenance": paper.get("selection_provenance_json"),
        "current_scoring_rubric": scoring_fingerprint(),
        "prompt": instructions
        if instructions is not None
        else skill_resource("analyze-report", "quality-review.md").read_text(
            encoding="utf-8"
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def pending_review(paper: dict, reason: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "tier": "pending",
        "label": TIER_LABELS["pending"],
        "status": "pending",
        "source_category": source_category(paper.get("source", "")),
        "relevance": current_relevance(paper),
        "relevance_reason": paper.get("selection_reason", ""),
        "quality": "unknown",
        "confidence": "low",
        "research_question": "",
        "method": None,
        "findings": [],
        "strengths": [],
        "limitations": [reason],
        "validation_issues": [reason],
        "critical_concerns": [],
        "reading_advice": "取得并核对原文后再评价研究质量。",
        "text_complete": False,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }


async def evaluate_paper(
    paper: dict, llm, *, max_chars: int = MAX_REVIEW_CHARS
) -> dict:
    text = paper.get("full_text") or ""
    if not paper.get("has_full_text") or len(text.strip()) < 1000:
        return pending_review(paper, "未取得可用全文；摘要只能用于相关性初筛。")
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if len(text) > max_chars:
        result = pending_review(
            paper,
            f"全文 {len(text)} 字符超过可用输入上限 {max_chars}；未发送部分文本，需提高限额或分批完整阅读。",
        )
        result.update(input_chars=0, full_text_chars=len(text))
        return result
    content = text
    chunks = text_chunks(content)
    instructions = skill_resource("analyze-report", "quality-review.md").read_text(
        encoding="utf-8"
    )
    prompt = json.dumps(
        {
            "title": paper.get("title"),
            "source_category": source_category(paper.get("source", "")),
            "abstract": paper.get("abstract"),
            "relevance_reason": paper.get("selection_reason"),
            "text_complete": len(content) == len(text),
            "text_chunks": chunks,
        },
        ensure_ascii=False,
    )
    data = await llm.chat_json(
        [
            {"role": "system", "content": instructions},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=6000,
    )
    review = Review.model_validate(data).model_dump(exclude_none=True)
    issues = []
    complete = len(content) == len(text)
    if not complete:
        issues.append("全文超过本次输入上限，当前仅完成部分文本评估。")
    relevance = current_relevance(paper)
    if review["quality"] == "unknown":
        issues.append("研究质量尚无法判断，需要补充材料或重新核验。")
    if paper.get("requires_screening_update"):
        issues.append("原文版本或摘要已更新，需要重新进行方向相关性初筛。")
    if (
        issues
        or not review["method"]
        or not review["findings"]
        or review["quality"] == "unknown"
    ):
        tier = "pending"
    elif review["quality"] == "weak" or review["critical_concerns"]:
        tier = "exclude"
    elif (
        review["quality"] in {"sound", "strong"}
        and review["confidence"] in {"medium", "high"}
        and relevance is not None
        and relevance >= 6
    ):
        tier = "focus"
    else:
        tier = "browse"
    return {
        **review,
        "schema_version": SCHEMA_VERSION,
        "tier": tier,
        "label": TIER_LABELS[tier],
        "status": "needs_verification" if tier == "pending" else "reviewed",
        "validation_issues": issues,
        "source_category": source_category(paper.get("source", "")),
        "text_complete": complete,
        "input_chars": len(content),
        "full_text_chars": len(text),
        "relevance": relevance,
        "relevance_reason": paper.get("selection_reason", "")
        if relevance is not None
        else "尚未按当前研究方向完成相关性初筛。",
        "model": llm.model,
        "fingerprint": review_fingerprint(
            paper, llm.model, max_chars, instructions=instructions
        ),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }


def select_candidates(
    papers: list[dict], max_reviews: int = 12, directions: list[str] | None = None
) -> list[dict]:
    """Round-robin directions after ranking within them, preserving academic priority."""
    ordered = sorted(
        papers,
        key=lambda p: (
            source_category(p.get("source", "")) != "academic",
            -(p.get("selection_relevance") or p.get("selection_score") or 0),
            p.get("published_date") or "",
        ),
    )
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


async def review_report_ids(
    report_ids: list[int], *, force: bool = False
) -> dict[int, dict]:
    """Read and save each assessment without holding a database transaction over HTTP."""
    from reportagent.db.engine import get_session_factory
    from reportagent.models.database import Report
    from reportagent.llm.client import LLMClient
    from reportagent.services.one_report import collect_paper

    fields = (
        "id",
        "source",
        "title",
        "authors",
        "abstract",
        "full_text",
        "has_full_text",
        "arxiv_id",
        "selection_score",
        "selection_relevance",
        "selection_reason",
        "selection_direction",
        "selection_provenance_json",
        "source_url",
        "published_date",
        "pdf_path",
    )

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
            refresh_failed = False
            cached = None
            try:
                try:
                    cached = json.loads(raw_cache or "null")
                except (ValueError, TypeError):
                    cached = None
                if not isinstance(cached, dict):
                    cached = None
                if (
                    (not paper["has_full_text"] or not paper["full_text"])
                    and paper["source"] == "arxiv"
                    and paper["arxiv_id"]
                ):
                    fetched = await collect_paper(paper["arxiv_id"])
                    with factory() as session:
                        row = session.get(Report, rid)
                        if row is None or snapshot(row) != paper:
                            results[rid] = pending_review(
                                paper, "获取期间论文或筛选结果已更新，请重新评估。"
                            )
                            continue
                        screening_changed = any(
                            getattr(fetched, key) != paper[key]
                            for key in ("title", "abstract", "arxiv_id")
                        )
                        for key in (
                            "title",
                            "full_text",
                            "abstract",
                            "arxiv_id",
                            "source_url",
                            "pdf_path",
                            "published_date",
                            "tables_json",
                            "equations_json",
                        ):
                            setattr(row, key, getattr(fetched, key))
                        row.authors = json.dumps(fetched.authors, ensure_ascii=False)
                        row.has_full_text = (
                            bool(fetched.full_text) and not fetched.abstract_only
                        )
                        row.analysis_json = row.evaluation_json = (
                            row.knowledge_card_json
                        ) = row.summary = None
                        if screening_changed:
                            row.selection_score = 0.0
                            row.selection_reason = row.selection_relevance = (
                                row.selection_direction
                            ) = row.selection_provenance_json = None
                            row.is_selected = False
                        session.commit()
                        paper = snapshot(row)
                    if screening_changed:
                        paper["requires_screening_update"] = True
                    cached = None
                fingerprint = review_fingerprint(paper, get_llm_model())
                if (
                    not force
                    and cached
                    and cached.get("fingerprint") == fingerprint
                    and cached.get("status") == "reviewed"
                    and cached.get("tier") != "pending"
                ):
                    results[rid] = cached
                    continue
                if (
                    not paper["has_full_text"]
                    or len((paper["full_text"] or "").strip()) < 1000
                ):
                    result = pending_review(
                        paper, "未取得可用全文；暂不作研究质量推荐。"
                    )
                else:
                    if llm is None:
                        llm = LLMClient(thinking="disabled")
                    result = await asyncio.wait_for(
                        evaluate_paper(paper, llm), timeout=240
                    )
            except Exception as exc:
                refresh_failed = True
                logger.warning("Report %s review failed: %s", rid, type(exc).__name__)
                result = pending_review(
                    paper, f"全文评估未完成（{type(exc).__name__}），可重试。"
                )
            with factory() as session:
                row = session.get(Report, rid)
                if row is None:
                    continue
                # Never attach results to a different evidence or scoring snapshot.
                if snapshot(row) != {key: paper[key] for key in fields}:
                    results[rid] = pending_review(
                        snapshot(row), "分析期间原文或筛选结果已更新，请重新评估。"
                    )
                    continue
                if (
                    refresh_failed
                    and cached
                    and cached.get("status") == "reviewed"
                    and cached.get("tier") != "pending"
                ):
                    result["previous_review_preserved"] = True
                else:
                    row.evaluation_json = json.dumps(result, ensure_ascii=False)
                    session.commit()
            results[rid] = result
    finally:
        if llm is not None:
            await llm._client.close()
    return results
