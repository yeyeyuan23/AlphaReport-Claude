"""Query the rolling seven-day publication window and deliver its report."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import logging
import os
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
)
from reportagent.services.feishu_markdown import card_sections
from reportagent.services.report_periods import (
    get_weekly_range,
    is_publication_in_range,
    format_publication_window,
    publication_column,
)

load_dotenv()

logger = logging.getLogger("weekly_report")

UI_BASE_URL = get_web_base_url()

# ═══════════════════════════════════════════════════
# Date helpers — all based on published_date
# ═══════════════════════════════════════════════════


@dataclass(frozen=True)
class WeeklyReportOptions:
    dry_run: bool = False
    with_summary: bool = False
    weeks_ago: int = 0
    receive_ids: str = ""
    receive_type: str = ""
    output: str | None = None


# ═══════════════════════════════════════════════════
# Query papers by published_date
# ═══════════════════════════════════════════════════


def get_weekly_papers(week_start: datetime, week_end: datetime) -> list[dict]:
    """Query papers with published_date in the inclusive calendar-date range."""
    from reportagent.sources.date_window import source_date
    week_start, week_end = source_date(week_start), source_date(week_end)
    init_db()
    factory = get_session_factory()
    session = factory()
    try:
        from reportagent.models.database import Report
        from sqlalchemy import desc

        q = session.query(Report).filter(
            publication_column() >= week_start,
            publication_column() <= week_end,
        )
        q = q.order_by(desc(Report.published_date), Report.id)
        reports = [
            r for r in q.all() if is_publication_in_range(r, week_start, week_end)
        ]

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


def generate_weekly_report(
    papers: list[dict],
    week_start: str,
    week_end: str,
    *,
    publication_window: str = "",
    summarize: bool = False,
) -> str:
    """Synthesize saved assessments without changing their quality judgments."""
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
    summaries, notice = None, ""
    if summarize:
        from reportagent.services.weekly_synthesis import synthesize_weekly

        try:
            summaries = asyncio.run(synthesize_weekly(checked))
        except Exception as error:
            logger.warning("Weekly synthesis unavailable: %s", type(error).__name__)
            notice = "模型总结未生成；以下逐篇展示已有研究发现与依据。"
    return render_briefing(
        checked,
        f"{week_start} ~ {week_end}",
        template_skill="weekly-report",
        publication_window=publication_window,
        weekly_summaries=summaries,
        summary_notice=notice,
    )


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
    text = generate_weekly_report(
        papers,
        start_label,
        end_label,
        publication_window=format_publication_window(week_start, week_end),
        summarize=not options.dry_run or options.with_summary,
    )
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
        "publication_window": {
            "start": week_start.isoformat(),
            "end": week_end.isoformat(),
            "end_exclusive": False,
        },
        "week_start": start_label,
        "week_end": end_label,
        "paper_ids": [p["id"] for p in papers],
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
    return result
