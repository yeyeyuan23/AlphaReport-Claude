"""Prepare knowledge cards from saved evidence; no additional model judgments."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from reportagent.db.engine import init_db, get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.models.schemas import KnowledgeCard, ReportListParams
from reportagent.services.knowledge_cards import (
    build_knowledge_card,
    get_knowledge_card,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NarrativeOptions:
    report_id: int | None = None
    recent: int | None = None
    force: bool = False
    dry_run: bool = False


async def generate_knowledge_card(
    llm,
    title,
    abstract,
    analysis_json,
    topics_str,
    asset_classes_str,
    *,
    evaluation_json=None,
    paper=None,
) -> KnowledgeCard:
    """Compatibility adapter. Analysis prose alone cannot substantiate findings."""
    snapshot = dict(paper or {})
    snapshot.update(
        title=title,
        abstract=abstract,
        analysis_json=analysis_json,
        topics=topics_str,
        asset_classes=asset_classes_str,
    )
    if evaluation_json is not None:
        snapshot["evaluation_json"] = evaluation_json
    return KnowledgeCard.model_validate(build_knowledge_card(snapshot))


async def run_narrative_brief(options: NarrativeOptions) -> dict:
    init_db()
    stats = {"ready": 0, "pending": 0}
    with get_session_factory()() as session:
        repo = ReportRepository(session)
        if options.report_id:
            report = repo.get_report(options.report_id)
            if report is None:
                raise ValueError("Report not found")
            reports = [report]
        else:
            reports, _ = repo.list_reports(
                ReportListParams(sort_by="created_at", limit=options.recent or 200)
            )
        for report in reports:
            card = (
                build_knowledge_card(report)
                if options.dry_run
                else get_knowledge_card(report, force=options.force)
            )
            stats[card["status"]] += 1
            logger.info("[%s] %s: %s", report.id, card["status"], card["notice"])
        if not options.dry_run:
            session.commit()
    return stats
