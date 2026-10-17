"""Shared report-reading workflows, independent of HTTP and wire serializers.

Adapters choose their existing summary serializer and whether detail includes a
knowledge card. Query semantics stay in the repository; freshness policies stay
in report_views and knowledge_cards. A missing report returns None for the
adapter to translate into its transport's not-found response.
"""

from collections.abc import Callable

from sqlalchemy.orm import Session

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Report
from reportagent.models.schemas import ReportListParams
from reportagent.services import knowledge_cards, report_views


ReportSerializer = Callable[[Report], dict]


def list_reports(
    db: Session, params: ReportListParams, *, serialize: ReportSerializer
) -> dict:
    reports, total = ReportRepository(db).list_reports(params)
    return {
        "reports": [serialize(report) for report in reports],
        "total": total,
        "limit": params.limit,
        "offset": params.offset,
    }


def get_report_detail(
    db: Session,
    report_id: int,
    *,
    serialize: ReportSerializer,
    include_knowledge_card: bool = False,
) -> dict | None:
    report = ReportRepository(db).get_report(report_id)
    if report is None:
        return None
    data = serialize(report)
    data.update(
        full_text=report.full_text,
        summary=report.summary,
        tables_json=report.tables_json,
        equations_json=report.equations_json,
    )
    analysis = report_views.read_current_analysis(report)
    if analysis is not None:
        data["analysis"] = analysis
    if include_knowledge_card:
        data["knowledge_card"] = knowledge_cards.build_knowledge_card(report)
    return data


def get_knowledge_card(
    db: Session, report_id: int, *, force: bool = False
) -> dict | None:
    report = ReportRepository(db).get_report(report_id)
    if report is None:
        return None
    previous = report.knowledge_card_json
    card = knowledge_cards.get_knowledge_card(report, force=force)
    if report.knowledge_card_json != previous:
        db.commit()
    return card
