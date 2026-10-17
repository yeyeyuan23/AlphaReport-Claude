"""Prepare knowledge cards from saved evidence; no additional model judgments."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from reportagent.db.engine import init_db, get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.models.schemas import ReportListParams
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
