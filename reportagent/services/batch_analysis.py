"""Analyze existing reports and persist their analysis and mapped knowledge cards."""

from __future__ import annotations

from dataclasses import dataclass

import asyncio
import json
import logging


from reportagent.services.report_views import map_analysis_to_knowledge_card as _map_analysis_to_knowledge_card

from reportagent.db.engine import init_db, get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.models.database import Report
from reportagent.llm.client import LLMClient
from reportagent.agents.analysis_agent import AnalysisAgent
from reportagent.services.one_report import validate_analysis

logger = logging.getLogger("analyze")


@dataclass(frozen=True)
class AnalysisOptions:
    all: bool = False
    unanalyzed: bool = False
    report_id: int | None = None
    recent: int | None = None
    depth: str = 'standard'
    force: bool = False
    topic: str | None = None


async def run_analysis(options: AnalysisOptions) -> dict:
    """Analyze a library selection, preserving successes when another paper fails.

    ``report_ids`` contains the entire selection, including cached and empty papers.
    ``analyzed_ids``, ``skipped_ids`` and ``failed_ids`` partition that selection.
    Only the current paper's model calls participate in its completeness check.
    """
    if options.topic is not None and (not options.topic.strip() or "," in options.topic):
        raise ValueError("topic must be one nonempty classification token")
    if options.recent is not None and options.recent < 1:
        raise ValueError("recent must be positive")
    init_db()
    factory = get_session_factory()
    session = factory()

    llm_client = None
    try:
        repo = ReportRepository(session)

        query = session.query(Report).order_by(Report.created_at.desc(), Report.id.desc())
        if options.report_id is not None:
            reports = [repo.get_report(options.report_id)]
            if not reports[0]:
                logger.error(f"Report {options.report_id} not found")
                raise ValueError("Report not found")
        elif options.recent is not None:
            reports = query.limit(options.recent).all()
        elif options.topic is not None:
            topic = options.topic.strip()
            # Repository topic search uses substring matching. Class batch selection
            # instead compares complete CSV tokens and scans beyond the first page.
            reports = [r for r in query.yield_per(200)
                       if topic in {token.strip() for token in (r.topics or "").split(",")}]
        elif options.unanalyzed:
            reports = [r for r in query.yield_per(200) if not r.analysis_json]
        else:
            reports = query.all()

        audit = {"selected": len(reports), "analyzed": 0, "skipped": 0, "failed": 0,
                 "report_ids": [r.id for r in reports], "analyzed_ids": [],
                 "skipped_ids": [], "failed_ids": [], "errors": {},
                 "evidence_scope_by_id": {}}

        if not reports:
            logger.info("No reports to analyze")
            return audit

        logger.info(f"Analyzing {len(reports)} reports (depth: {options.depth})...")

        agent = None
        for i, r in enumerate(reports):
            evidence_scope = "full_text" if r.has_full_text and (r.full_text or "").strip() else "abstract"
            audit["evidence_scope_by_id"][r.id] = evidence_scope
            if r.analysis_json and not options.force:
                audit["skipped_ids"].append(r.id)
                continue

            full_text = (r.full_text if evidence_scope == "full_text" else r.abstract) or ""
            if not any(character.isalnum() for character in full_text):
                logger.warning(f"  [{i+1}/{len(reports)}] SKIP (no text): {r.title[:50]}")
                audit["skipped_ids"].append(r.id)
                continue

            try:
                if llm_client is None:
                    llm_client = LLMClient()
                    agent = AnalysisAgent(llm_client=llm_client)
                call_start = len(llm_client.calls)
                result = await agent.analyze_report_directly(
                    title=r.title,
                    abstract=r.abstract or "",
                    full_text=full_text,
                    depth=options.depth,
                )
                if options.depth == "quick":
                    if not all(value.strip() for value in (
                        result.research_question, result.core_contribution, result.summary
                    )):
                        raise ValueError("分析不完整：研究问题、核心贡献或总结为空")
                else:
                    result = validate_analysis(result.model_dump())
                calls = llm_client.calls[call_start:]
                if any(call.get("complete") is not True for call in calls):
                    raise ValueError("存在失败或截断的模型调用，停止保存本篇分析")
                analysis_dict = result.model_dump()
                analysis_dict.update(evidence_scope=evidence_scope, model_calls=calls)
                kc = _map_analysis_to_knowledge_card(analysis_dict)
                kc["topics"] = [t.strip() for t in (r.topics or "").split(",") if t.strip()]
                kc["asset_classes"] = [t.strip() for t in (r.asset_classes or "").split(",") if t.strip()]
                kc["evidence_scope"] = evidence_scope
                # Save one coherent paper snapshot; rollback cannot undo siblings.
                r.analysis_json = json.dumps(analysis_dict, ensure_ascii=False, indent=2)
                r.knowledge_card_json = json.dumps(kc, ensure_ascii=False)
                session.commit()
                audit["analyzed_ids"].append(r.id)
                logger.info(f"  [{i+1}/{len(reports)}] ✓ {r.title[:50]}")
            except Exception as e:
                session.rollback()
                audit["failed_ids"].append(r.id)
                audit["errors"][r.id] = str(e)
                logger.warning(f"  [{i+1}/{len(reports)}] ✗ {r.title[:50]}: {e}")

            # Rate limit
            await asyncio.sleep(0.2)

        for status in ("analyzed", "skipped", "failed"):
            audit[status] = len(audit[f"{status}_ids"])
        logger.info("Analyzed: %s, Skipped: %s, Failed: %s, Total: %s",
                    audit["analyzed"], audit["skipped"], audit["failed"], audit["selected"])
        return audit

    finally:
        try:
            if llm_client is not None:
                await llm_client._client.close()
        finally:
            session.close()
