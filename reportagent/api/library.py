"""Report reading API backed by shared repositories and presentation services."""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from reportagent.db.engine import get_db
from reportagent.services.report_views import read_current_analysis
from reportagent.services import report_pdf
from reportagent.sources.content_fetch import get_pdf_policy
from reportagent.db.repository import ReportRepository
from reportagent.models.schemas import ReportListParams
from reportagent.utils.config import PROJECT_ROOT
from reportagent.skills.registry import skill_resource
from reportagent.services.report_views import (
    report_to_dict as _report_to_dict,
    map_analysis_to_briefing_sections as _map_analysis_to_briefing_sections,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["library"])

_RESEARCH_BRIEFING_PROMPT = skill_resource("analyze-report", "briefing.md").read_text(
    encoding="utf-8"
)


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
    reports, total = ReportRepository(db).list_reports(params)
    return {
        "success": True,
        "data": {
            "reports": [_report_to_dict(r) for r in reports],
            "total": total,
            "limit": limit,
            "offset": offset,
        },
    }


@router.get("/reports/{report_id}")
def get_report(report_id: int, db: Session = Depends(get_db)):
    report = ReportRepository(db).get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    data = _report_to_dict(report)
    data["full_text"] = report.full_text
    data["summary"] = report.summary
    data["tables_json"] = report.tables_json
    data["equations_json"] = report.equations_json
    analysis = read_current_analysis(report)
    if analysis is not None:
        data["analysis"] = analysis
    from reportagent.services.knowledge_cards import build_knowledge_card

    data["knowledge_card"] = build_knowledge_card(report)
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
    from reportagent.services.knowledge_cards import get_knowledge_card as prepare_card

    report = ReportRepository(db).get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    previous = report.knowledge_card_json
    card = prepare_card(report, force=force)
    if report.knowledge_card_json != previous:
        db.commit()
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

    # 1) Client-side mapping is preferred — return analysis_json so the
    #    frontend ResearchBriefing component can build sections directly.
    #    This endpoint provides the LLM-generated narrative version.
    analysis = read_current_analysis(report)
    if analysis:
        try:
            # Build context for LLM
            ctx_parts = []
            if analysis.get("research_question"):
                ctx_parts.append(f"研究问题：{analysis['research_question']}")
            if analysis.get("core_contribution"):
                ctx_parts.append(f"核心贡献：{analysis['core_contribution']}")
            if analysis.get("method_category"):
                ctx_parts.append(f"方法类别：{analysis['method_category']}")
            methodology = analysis.get("methodology") or {}
            for pt in methodology.get("analysis_points", []):
                if isinstance(pt, dict) and pt.get("analysis"):
                    ctx_parts.append(
                        f"分析点-{pt.get('title', '')}：{pt['analysis'][:500]}"
                    )
            assessment = analysis.get("assessment") or {}
            if assessment.get("strengths"):
                ctx_parts.append(f"优势：{'；'.join(assessment['strengths'])}")
            if assessment.get("weaknesses"):
                ctx_parts.append(f"不足：{'；'.join(assessment['weaknesses'])}")
            if assessment.get("marginal_contribution_summary"):
                ctx_parts.append(
                    f"边际贡献：{assessment['marginal_contribution_summary']}"
                )
            if assessment.get("practical_implications"):
                ctx_parts.append(
                    f"实践启示：{'；'.join(assessment['practical_implications'])}"
                )

            analysis_context = "\n".join(ctx_parts)

            # If force=true, generate LLM narrative
            if force:
                try:
                    from reportagent.llm.client import LLMClient

                    llm = LLMClient()
                    prompt = _RESEARCH_BRIEFING_PROMPT.format(
                        title=report.title,
                        abstract=report.abstract or "",
                        analysis_context=analysis_context,
                    )
                    narrative = await llm.chat(
                        [
                            {
                                "role": "system",
                                "content": "你是量化金融研究简报撰写专家，仿照 QuantML 风格写叙事性研报解读。全部中文输出。",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.5,
                        max_tokens=3000,
                    )
                    return {
                        "success": True,
                        "data": {"content": narrative, "source": "llm"},
                    }
                except Exception as e:
                    logger.warning(f"LLM briefing generation failed: {e}")

            # Fallback: return mapped sections
            sections = _map_analysis_to_briefing_sections(analysis)
            return {"success": True, "data": {"sections": sections, "source": "mapped"}}
        except (json.JSONDecodeError, Exception):
            pass

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
