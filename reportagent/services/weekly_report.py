"""Query the rolling seven-day publication window and deliver its report."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import asyncio
from datetime import datetime
from pathlib import Path


from dotenv import load_dotenv


from reportagent.db.engine import init_db, get_session_factory
from reportagent.utils.config import get_llm_model
from reportagent.services.report_briefing import render_briefing
from reportagent.services.public_url import get_web_base_url
from reportagent.services.report_quality import (
    pending_review,
    review_fingerprint,
    cached_review_is_current,
    resolve_review_budget,
)
from reportagent.services.feishu_markdown import card_sections
from reportagent.services.report_periods import get_weekly_range

load_dotenv()

logger = logging.getLogger("weekly_report")

UI_BASE_URL = get_web_base_url()

# ═══════════════════════════════════════════════════
# Date helpers — all based on published_date
# ═══════════════════════════════════════════════════


@dataclass(frozen=True)
class WeeklyReportOptions:
    dry_run: bool = False
    weeks_ago: int = 0
    receive_ids: str = ""
    receive_type: str = ""
    output: str | None = None
    no_llm: bool = False
    max_reviews: int | None = None
    force_review: bool = False


# ═══════════════════════════════════════════════════
# Query papers by published_date
# ═══════════════════════════════════════════════════


def get_weekly_papers(week_start: datetime, week_end: datetime) -> list[dict]:
    """Query papers with published_date in [week_start, week_end]."""
    init_db()
    factory = get_session_factory()
    session = factory()
    try:
        from reportagent.models.database import Report
        from sqlalchemy import desc

        q = session.query(Report).filter(
            Report.published_date >= week_start,
            Report.published_date <= week_end,
        )
        q = q.order_by(desc(Report.published_date), Report.id)
        reports = q.all()

        papers = []
        for r in reports:
            papers.append(
                {
                    "id": r.id,
                    "title": r.title or "",
                    "authors": r.authors or "",
                    "abstract": r.abstract or "",
                    "full_text": r.full_text or "",
                    "source": r.source or "",
                    "material_type": r.material_type or "unknown",
                    "publication_date_info": json.loads(r.publication_date_info_json)
                    if r.publication_date_info_json
                    else {},
                    "source_url": r.source_url or "",
                    "arxiv_id": r.arxiv_id or "",
                    "doi": r.doi or "",
                    "topics": r.topics or "",
                    "published_date": r.published_date.isoformat()
                    if r.published_date
                    else "",
                    "has_full_text": r.has_full_text,
                    "selection_score": r.selection_score or 0.0,
                    "is_selected": bool(r.is_selected),
                    "selection_relevance": r.selection_relevance,
                    "selection_direction": r.selection_direction,
                    "selection_reason": r.selection_reason,
                    "selection_provenance_json": r.selection_provenance_json,
                    "evaluation_json": r.evaluation_json,
                    "pdf_url": f"https://arxiv.org/pdf/{r.arxiv_id}"
                    if r.arxiv_id
                    else "",
                    "ui_url": f"{UI_BASE_URL}/library/{r.id}",
                }
            )

        logger.info(
            f"Found {len(papers)} papers published between "
            f"{week_start.strftime('%Y-%m-%d')} and {week_end.strftime('%Y-%m-%d')}"
        )
        return papers
    finally:
        session.close()


# ═══════════════════════════════════════════════════
# LLM Weekly Report Generation
# ═══════════════════════════════════════════════════


def generate_weekly_report(papers: list[dict], week_start: str, week_end: str) -> str:
    """Render current saved assessments; never generate a second research judgment."""
    checked = []
    for paper in papers:
        p = dict(paper)
        evaluation = p.get("evaluation")
        if not isinstance(evaluation, dict):
            try:
                evaluation = json.loads(p.get("evaluation_json") or "null")
            except (ValueError, TypeError):
                evaluation = None
        current_pending = (
            isinstance(evaluation, dict)
            and evaluation.get("tier") == "pending"
            and evaluation.get("status") in {"pending", "needs_verification"}
            and evaluation.get("model") == get_llm_model()
            and evaluation.get("fingerprint") == review_fingerprint(p, get_llm_model())
        )
        if not current_pending and not cached_review_is_current(
            p, evaluation, get_llm_model()
        ):
            evaluation = pending_review(
                p, "尚无与当前原文、模型和筛选标准一致的全文评估。"
            )
        p["evaluation"] = evaluation
        checked.append(p)
    return render_briefing(
        checked, f"{week_start} ~ {week_end}", template_skill="weekly-report"
    )


async def prepare_weekly_evidence(
    papers: list[dict], *, max_reviews: int | None = None, force_review: bool = False
) -> dict:
    """Refresh relevance and complete reviews through the same Python API as daily."""
    from reportagent.llm.client import LLMClient
    from reportagent.services.paper_scoring import (
        screening_is_current,
        has_screening_material,
        score_papers_batch,
        apply_scores,
        _load_scoring_config,
        _configured_threshold,
    )
    from reportagent.services.report_quality import review_report_ids, select_candidates

    max_reviews = resolve_review_budget(max_reviews)
    stale = [p for p in papers if not screening_is_current(p)]
    audit = {
        "screened": 0,
        "pending_needs_material": 0,
        "review_ids": [],
        "failures": [],
    }
    if stale:
        client = None
        try:
            if any(has_screening_material(p) for p in stale):
                client = LLMClient()
            results, failures = await score_papers_batch(client, stale, batch_size=5)
            stats = apply_scores(results)
            audit.update(
                screened=stats["total_scored"],
                pending_needs_material=stats["pending_needs_material"],
            )
            audit["failures"].extend(
                {"stage": "screening", "count": n, "error": error}
                for _, n, error in failures
            )
            if stats["errors"]:
                audit["failures"].append(
                    {"stage": "persistence", "count": stats["errors"]}
                )
        except Exception as error:
            audit["failures"].append(
                {"stage": "screening", "error": type(error).__name__}
            )
            logger.warning("Weekly screening failed: %s", type(error).__name__)
        finally:
            if client is not None:
                await client._client.close()
    # Reload only the requested IDs so partial scoring failures cannot use stale scores.
    from reportagent.models.database import Report

    with get_session_factory()() as session:
        ids = [p["id"] for p in papers]
        rows = session.query(Report).filter(Report.id.in_(ids)).all()
        threshold = _configured_threshold(_load_scoring_config())
        candidates = []
        for row in rows:
            if (
                not screening_is_current(row)
                or row.selection_relevance is None
                or row.selection_relevance < threshold
            ):
                continue
            snapshot = dict(vars(row))
            try:
                cached = json.loads(row.evaluation_json or "null")
            except (ValueError, TypeError):
                cached = None
            if not force_review and cached_review_is_current(
                snapshot, cached, get_llm_model()
            ):
                continue  # Saved reviews remain visible without consuming new-review budget.
            candidates.append(snapshot)
    audit["unreviewed_candidates"] = len(candidates)
    selected = select_candidates(candidates, max_reviews=len(candidates))
    review_audit = {}
    reviewed = (
        await review_report_ids(
            [p["id"] for p in selected],
            force=force_review,
            max_reviews=max_reviews,
            audit=review_audit,
        )
        if selected
        else {}
    )
    audit.update(review_audit)
    audit["review_ids"] = review_audit.get("review_ids", list(reviewed))
    audit["review_tiers"] = {
        str(rid): result.get("tier") for rid, result in reviewed.items()
    }
    for rid, result in reviewed.items():
        if result.get("status") != "reviewed":
            audit["failures"].append(
                {
                    "stage": "review",
                    "id": rid,
                    "reason": result.get("validation_issues", []),
                }
            )
    return audit


# ═══════════════════════════════════════════════════
# Send to Feishu
# ═══════════════════════════════════════════════════


def send_feishu_card(
    receive_id: str,
    report_text: str,
    papers: list[dict],
    week_start: str,
    week_end: str,
    receive_type: str = "chat_id",
):
    """Send interactive card to Feishu."""

    elements = []

    # Header
    elements.append(
        {
            "tag": "markdown",
            "content": f"**📊 量化研报周报**\n{week_start} ~ {week_end} ｜ 共收录 {len(papers)} 篇论文",
        }
    )

    # Use the same section spacing as daily cards.
    elements.extend(card_sections(report_text, omit_leading_title=True))

    # Footer
    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "AlphaReport 周报 · 复用原文证据评估"}
            ],
        }
    )

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"📊 AlphaReport 周报 · {week_start} ~ {week_end}",
            },
            "template": "wathet",
        },
        "elements": elements,
    }

    from reportagent.services.feishu_transport import (
        build_client,
        send_card_with_fallback,
    )

    try:
        return send_card_with_fallback(
            build_client(),
            card,
            report_text,
            receive_id,
            receive_type=receive_type,
        )
    except Exception:
        logger.warning("Feishu weekly card delivery failed")
        return False


# ═══════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════


def run_weekly_report(options: WeeklyReportOptions) -> dict:
    from reportagent.services.paper_scoring import _load_scoring_config

    max_reviews = resolve_review_budget(
        options.max_reviews, _load_scoring_config().get("push_settings", {})
    )
    week_start, week_end, start_label, end_label = get_weekly_range(options.weeks_ago)
    weekly_ids = os.environ.get("FEISHU_WEEKLY_PUSH_IDS", "").strip()
    configured_ids = weekly_ids or os.environ.get("FEISHU_DAILY_PUSH_IDS", "")
    receive_ids = [
        i.strip()
        for i in (options.receive_ids or configured_ids).split(",")
        if i.strip()
    ]
    type_env = "FEISHU_WEEKLY_PUSH_TYPE" if weekly_ids else "FEISHU_DAILY_PUSH_TYPE"
    receive_type = options.receive_type or os.environ.get(
        type_env, "chat_id" if weekly_ids else "open_id"
    )
    if not options.dry_run and not receive_ids:
        raise ValueError(
            "Set FEISHU_WEEKLY_PUSH_IDS or FEISHU_DAILY_PUSH_IDS before delivery"
        )
    papers = get_weekly_papers(week_start, week_end)
    preparation = {}
    if papers and not options.no_llm:
        preparation = asyncio.run(
            prepare_weekly_evidence(
                papers,
                max_reviews=max_reviews,
                force_review=options.force_review,
            )
        )
        papers = get_weekly_papers(week_start, week_end)
    text = generate_weekly_report(papers, start_label, end_label)
    failures = preparation.get("failures", [])
    if failures:
        text += f"\n\n本期有 {len(failures)} 项筛选或全文阅读评估未完成；相关条目保留待核验，可重试。"
    unattempted = len(preparation.get("deferred_ids", []))
    if unattempted > 0:
        text += f"\n\n本次全文评估预算为 {max_reviews} 篇，另有 {unattempted} 篇候选尚未评估；重跑可继续处理。"
    if options.output:
        output = Path(options.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    result = {
        "status": "preview"
        if options.dry_run
        else "empty"
        if not papers
        else "complete",
        "week_start": start_label,
        "week_end": end_label,
        "paper_ids": [p["id"] for p in papers],
        "model_refresh": not options.no_llm,
        "preparation": preparation,
        "review_budget": max_reviews,
        "sent": 0,
        "failed_recipients": [],
    }
    if options.dry_run:
        print(text)
        return result
    if not papers:
        logger.info("No published papers in week %s ~ %s", start_label, end_label)
        return result
    for rid in receive_ids:
        if send_feishu_card(rid, text, papers, start_label, end_label, receive_type):
            result["sent"] += 1
        else:
            result["failed_recipients"].append(rid)
    if result["failed_recipients"]:
        result["status"] = "partial" if result["sent"] else "failed"
    elif failures:
        result["status"] = "partial"
    return result
