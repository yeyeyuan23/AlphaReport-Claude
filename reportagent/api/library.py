"""Report reading API backed by shared repositories and presentation services."""

import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from reportagent.db.engine import get_db
from reportagent.services.report_views import read_current_analysis
from reportagent.services import report_pdf, report_reads
from reportagent.sources.content_fetch import get_pdf_policy
from reportagent.db.repository import ReportRepository
from reportagent.models.schemas import ReportListParams
from reportagent.utils.config import PROJECT_ROOT
from reportagent.services.narrative_briefing import build_briefing_messages
from reportagent.services.report_views import (
    report_to_dict as _report_to_dict,
    map_analysis_to_briefing_sections as _map_analysis_to_briefing_sections,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["library"])


@router.get("/reports/stats")
def get_stats(db: Session = Depends(get_db)):
    stats = ReportRepository(db).get_stats()
    return {"success": True, "data": stats}


@router.get("/reports")
def list_reports(
    market: Optional[str] = Query(None),
    asset_class: Optional[str] = Query(None),
    frequency: Optional[str] = Query(None),
    topic: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    has_full_text: Optional[bool] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    sort_by: str = Query("created_at"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    params = ReportListParams(
        market=market,
        asset_class=asset_class,
        frequency=frequency,
        topic=topic,
        search=search,
        source=source,
        has_full_text=has_full_text,
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
    )
    return {
        "success": True,
        "data": report_reads.list_reports(db, params, serialize=_report_to_dict),
    }


@router.get("/reports/{report_id}")
def get_report(report_id: int, db: Session = Depends(get_db)):
    data = report_reads.get_report_detail(
        db, report_id, serialize=_report_to_dict, include_knowledge_card=True
    )
    if data is None:
        raise HTTPException(status_code=404, detail="Report not found")
    data["briefing_sections"] = _map_analysis_to_briefing_sections(
        data.get("analysis") or {}
    )
    return {"success": True, "data": data}


@router.get("/reports/{report_id}/pdf")
def get_report_pdf(report_id: int, db: Session = Depends(get_db)):
    report = ReportRepository(db).get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    try:
        path = report_pdf.cached_pdf_path(report)
        if path is None and report.source == "arxiv" and report.source_url:
            policy = get_pdf_policy()
            snapshot = report_pdf.pdf_snapshot(report)
            data = report_pdf.download_pdf_sync(snapshot, policy)
            path = report_pdf.save_pdf(
                db, snapshot, data, PROJECT_ROOT / "data" / "pdfs"
            )
        if path is not None:
            return FileResponse(
                path,
                media_type="application/pdf",
                filename=f"report_{report_id}.pdf",
                content_disposition_type="inline",
            )
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="No PDF available for this report")


@router.post("/reports/{report_id}/knowledge-card")
def get_knowledge_card(
    report_id: int,
    force: bool = Query(False, description="刷新当前证据的卡片展示；不重新评估论文"),
    db: Session = Depends(get_db),
):
    card = report_reads.get_knowledge_card(db, report_id, force=force)
    if card is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"success": True, "data": card}


@router.post("/reports/{report_id}/research-briefing")
async def get_research_briefing(
    report_id: int,
    force: bool = Query(False, description="强制重新生成"),
    db: Session = Depends(get_db),
):
    report = ReportRepository(db).get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    # Normal reads share the detail view's deterministic presentation.
    # Only an explicit force request may ask a model for a new narrative.
    analysis = read_current_analysis(report)
    if analysis:
        if not force:
            return {
                "success": True,
                "data": {
                    "sections": _map_analysis_to_briefing_sections(analysis),
                    "source": "mapped",
                },
            }
        try:
            from reportagent.llm.client import LLMClient

            messages = build_briefing_messages(report, analysis)
            llm = LLMClient()
            narrative = await llm.chat(messages, temperature=0.2, max_tokens=3000)
            return {
                "success": True,
                "data": {"content": narrative, "source": "llm"},
            }
        except Exception as exc:
            logger.warning("LLM briefing generation failed: %s", type(exc).__name__)
            return {
                "success": True,
                "data": {
                    "sections": _map_analysis_to_briefing_sections(analysis),
                    "source": "mapped",
                },
            }

    # 2) No analysis available
    return {
        "success": True,
        "data": {
            "sections": [],
            "content": "",
            "source": "empty",
            "error": "No analysis data available. Run AI analysis first.",
        },
    }


@router.get("/classification/taxonomy")
def get_taxonomy():
    taxonomy_path = PROJECT_ROOT / "configs" / "classification_taxonomy.yaml"
    if taxonomy_path.exists():
        import yaml

        with open(taxonomy_path) as f:
            return {"success": True, "data": yaml.safe_load(f)}
    return {"success": True, "data": {}}
