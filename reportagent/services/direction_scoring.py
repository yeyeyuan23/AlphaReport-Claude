"""Score stored papers against configured research directions."""

from __future__ import annotations

from dataclasses import dataclass

import json
import logging
from datetime import datetime, timezone, timedelta


from reportagent.db.engine import init_db, get_session_factory
from reportagent.filters.direction_filter import DirectionFilter, PaperDirectionResult

logger = logging.getLogger("llm_direction_filter")


@dataclass(frozen=True)
class DirectionOptions:
    days: int = 1
    report_ids: str = ""
    threshold: float | None = None
    dry_run: bool = False
    json_output: bool = False


def get_papers(days: int = 1, report_ids: list[int] | None = None) -> list[dict]:
    """从 DB 获取待评分的论文。"""
    init_db()
    factory = get_session_factory()
    session = factory()
    try:
        from reportagent.models.database import Report
        from sqlalchemy import desc

        q = session.query(Report)

        if report_ids:
            q = q.filter(Report.id.in_(report_ids))
        else:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            q = q.filter(Report.created_at >= since)

        q = q.order_by(desc(Report.created_at))
        reports = q.all()

        papers = []
        for r in reports:
            papers.append(
                {
                    "id": r.id,
                    "title": r.title or "",
                    "authors": r.authors or "",
                    "abstract": r.abstract or "",
                    "source": r.source or "",
                    "source_url": r.source_url or "",
                    "arxiv_id": r.arxiv_id or "",
                    "topics": r.topics or "",
                    "published_date": r.published_date.isoformat()
                    if r.published_date
                    else "",
                }
            )
        logger.info(
            "Fetched %d papers from DB (days=%d, ids=%s)", len(papers), days, report_ids
        )
        return papers
    finally:
        session.close()


def store_scores(results: list[PaperDirectionResult]):
    """Persist canonical selections; direction details live in their provenance."""
    from reportagent.services.paper_scoring import apply_scores

    stats = {"total_scored": 0, "errors": 0}
    for result in results:
        if result.status == "retryable_error" or result.assessment is None:
            continue
        saved = apply_scores([result.assessment], threshold=result.threshold)
        stats["total_scored"] += saved["total_scored"]
        stats["errors"] += saved["errors"]
    return stats


async def run_direction_filter(options: DirectionOptions) -> None:
    # Resolve papers
    report_ids = None
    if options.report_ids:
        report_ids = [
            int(i.strip()) for i in options.report_ids.split(",") if i.strip()
        ]

    papers = get_papers(days=options.days, report_ids=report_ids)
    if not papers:
        logger.warning("No papers found to score")
        return

    # Score
    filt = DirectionFilter()
    if options.threshold is not None:
        from reportagent.services.paper_scoring import _valid_score

        filt.SELECTION_THRESHOLD = _valid_score(options.threshold, "threshold")
    results = await filt.score_papers(papers)

    # Output
    selected = [r for r in results if r.is_selected]
    output = {
        "total": len(results),
        "selected": len(selected),
        "threshold": filt.SELECTION_THRESHOLD,
        "results": [
            {
                "report_id": r.report_id,
                "title": r.title[:80],
                "max_score": r.max_score,
                "best_direction": r.best_direction,
                "selected": r.is_selected,
                "status": r.status,
                "reason": r.reason,
                "threshold": r.threshold,
                "scores": [
                    {
                        "direction": s.direction_name,
                        "score": s.score,
                        "reason": s.reason,
                    }
                    for s in sorted(
                        r.direction_scores, key=lambda x: x.score, reverse=True
                    )
                    if s.score > 0
                ],
            }
            for r in sorted(
                results,
                key=lambda x: x.max_score if x.max_score is not None else -1,
                reverse=True,
            )
        ],
    }

    if options.json_output:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'=' * 60}")
        print(f"📊 LLM 方向过滤结果: {len(selected)}/{len(results)} 篇入选")
        print(f"{'=' * 60}")
        for r in output["results"]:
            icon = "⭐" if r["selected"] else "  "
            print(
                f"\n{icon} [{r['max_score'] if r['max_score'] is not None else r['status']}] {r['title']}"
            )
            if r["scores"]:
                for s in r["scores"][:3]:
                    print(f"     {s['direction']}: {s['score']:.1f} — {s['reason']}")

    # Store
    if not options.dry_run:
        store_scores(results)
        logger.info("Scores stored to DB")
    else:
        logger.info("[DRY RUN] Scores not stored")
