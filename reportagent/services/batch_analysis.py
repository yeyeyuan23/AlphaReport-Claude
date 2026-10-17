"""Analyze existing reports and persist their analysis and mapped knowledge cards."""

from __future__ import annotations

from dataclasses import dataclass

import asyncio
import json
import hashlib
import logging

from sqlalchemy import update


from reportagent.db.engine import init_db, get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.models.database import Report
from reportagent.llm.client import LLMClient
from reportagent.agents.analysis_agent import AnalysisAgent
from reportagent.services.one_report import validate_analysis
from reportagent.services.paper_scoring import has_screening_material
from reportagent.skills.registry import skill_resource
from reportagent.utils.config import get_llm_model, get_llm_base_url, get_llm_provider

logger = logging.getLogger("analyze")

_ANALYSIS_INPUT_FIELDS = (
    "title",
    "abstract",
    "full_text",
    "has_full_text",
    "arxiv_id",
    "equations_json",
)


def analysis_cache_fingerprint(report, depth: str) -> str:
    """A cache represents this purpose, source snapshot and effective model policy."""
    resources = {
        name: skill_resource("analyze-report", name).read_text(encoding="utf-8")
        for name in ("analysis.yaml", "summary.yaml", "grounding.md")
    }
    payload = {
        "schema": 1,
        "depth": depth,
        "model": get_llm_model(),
        "provider": get_llm_provider(),
        "endpoint": get_llm_base_url(),
        "resources": resources,
        "paper": {key: getattr(report, key, None) for key in _ANALYSIS_INPUT_FIELDS},
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _usable_cache(raw: str | None, fingerprint: str, depth: str) -> bool:
    try:
        data = json.loads(raw or "null")
        if (
            not isinstance(data, dict)
            or data.get("analysis_fingerprint") != fingerprint
            or data.get("depth") != depth
        ):
            return False
        if depth == "quick":
            return all(
                isinstance(data.get(key), str) and data[key].strip()
                for key in ("research_question", "core_contribution", "summary")
            )
        validate_analysis(data)
        return True
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class AnalysisOptions:
    all: bool = False
    unanalyzed: bool = False
    report_id: int | None = None
    recent: int | None = None
    depth: str = "standard"
    force: bool = False
    topic: str | None = None


async def run_analysis(options: AnalysisOptions) -> dict:
    """Analyze a library selection, preserving successes when another paper fails.

    ``report_ids`` contains the entire selection, including cached and empty papers.
    ``analyzed_ids``, ``skipped_ids`` and ``failed_ids`` partition that selection.
    Only the current paper's model calls participate in its completeness check.
    """
    if options.depth not in {"quick", "standard", "deep"}:
        raise ValueError("depth must be quick, standard or deep")
    if options.topic is not None and (
        not options.topic.strip() or "," in options.topic
    ):
        raise ValueError("topic must be one nonempty classification token")
    if options.recent is not None and options.recent < 1:
        raise ValueError("recent must be positive")
    init_db()
    factory = get_session_factory()
    session = factory()

    llm_client = None
    try:
        repo = ReportRepository(session)

        query = session.query(Report).order_by(
            Report.created_at.desc(), Report.id.desc()
        )
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
            reports = [
                r
                for r in query.yield_per(200)
                if topic in {token.strip() for token in (r.topics or "").split(",")}
            ]
        elif options.unanalyzed:
            reports = [r for r in query.yield_per(200) if not r.analysis_json]
        else:
            reports = query.all()

        audit = {
            "selected": len(reports),
            "analyzed": 0,
            "skipped": 0,
            "failed": 0,
            "report_ids": [r.id for r in reports],
            "analyzed_ids": [],
            "skipped_ids": [],
            "failed_ids": [],
            "errors": {},
            "evidence_scope_by_id": {},
        }

        if not reports:
            logger.info("No reports to analyze")
            return audit

        logger.info(f"Analyzing {len(reports)} reports (depth: {options.depth})...")

        # Select identities once, then read each input in a short-lived session.
        # No database transaction remains open while awaiting a model.
        session.close()
        agent = None
        agent_policy = None
        for i, report_id in enumerate(audit["report_ids"]):
            with factory() as read_session:
                r = read_session.get(Report, report_id)
            if r is None:
                audit["failed_ids"].append(report_id)
                audit["errors"][report_id] = "Report no longer exists"
                continue
            original = {
                key: getattr(r, key)
                for key in (*_ANALYSIS_INPUT_FIELDS, "analysis_json")
            }
            evidence_scope = (
                "full_text"
                if r.has_full_text and (r.full_text or "").strip()
                else "abstract"
            )
            audit["evidence_scope_by_id"][r.id] = evidence_scope
            fingerprint = analysis_cache_fingerprint(r, options.depth)
            if not options.force and _usable_cache(
                r.analysis_json, fingerprint, options.depth
            ):
                audit["skipped_ids"].append(r.id)
                continue

            full_text = (
                r.full_text if evidence_scope == "full_text" else r.abstract
            ) or ""
            if not has_screening_material({"title": r.title, "abstract": full_text}):
                logger.warning(
                    f"  [{i + 1}/{len(reports)}] SKIP (no text): {r.title[:50]}"
                )
                audit["skipped_ids"].append(r.id)
                continue

            try:
                current_policy = analysis_cache_fingerprint(None, options.depth)
                if llm_client is None:
                    agent_policy = current_policy
                    llm_client = LLMClient()
                    agent = AnalysisAgent(llm_client=llm_client)
                elif current_policy != agent_policy:
                    raise ValueError("分析期间模型或规则已更新，请重试")
                call_start = len(llm_client.calls)
                result = await agent.analyze_report_directly(
                    title=r.title,
                    abstract=r.abstract or "",
                    full_text=full_text,
                    depth=options.depth,
                    equations_json=r.equations_json,
                )
                if options.depth == "quick":
                    if not all(
                        value.strip()
                        for value in (
                            result.research_question,
                            result.core_contribution,
                            result.summary,
                        )
                    ):
                        raise ValueError("分析不完整：研究问题、核心贡献或总结为空")
                else:
                    result = validate_analysis(result.model_dump())
                calls = llm_client.calls[call_start:]
                if any(call.get("complete") is not True for call in calls):
                    raise ValueError("存在失败或截断的模型调用，停止保存本篇分析")
                analysis_dict = result.model_dump()
                analysis_dict.update(
                    evidence_scope=evidence_scope,
                    model_calls=calls,
                    depth=options.depth,
                    analysis_fingerprint=fingerprint,
                )
                if analysis_cache_fingerprint(r, options.depth) != fingerprint:
                    raise ValueError("分析期间模型或规则已更新，请重试")
                # Compare and write in one statement: a later source revision or
                # completed analysis must never be overwritten by this request.
                with factory() as write_session:
                    saved = write_session.execute(
                        update(Report)
                        .where(
                            Report.id == report_id,
                            *(
                                getattr(Report, key) == value
                                for key, value in original.items()
                            ),
                        )
                        .values(
                            analysis_json=json.dumps(
                                analysis_dict, ensure_ascii=False, indent=2
                            )
                        )
                    )
                    if saved.rowcount != 1:
                        raise ValueError("分析期间原文或已有分析已更新，请重试")
                    from reportagent.services.knowledge_cards import get_knowledge_card

                    get_knowledge_card(write_session.get(Report, report_id))
                    write_session.commit()
                audit["analyzed_ids"].append(r.id)
                logger.info(f"  [{i + 1}/{len(reports)}] ✓ {r.title[:50]}")
            except Exception as e:
                audit["failed_ids"].append(r.id)
                audit["errors"][r.id] = str(e)
                logger.warning(f"  [{i + 1}/{len(reports)}] ✗ {r.title[:50]}: {e}")

            # Rate limit
            await asyncio.sleep(0.2)

        for status in ("analyzed", "skipped", "failed"):
            audit[status] = len(audit[f"{status}_ids"])
        logger.info(
            "Analyzed: %s, Skipped: %s, Failed: %s, Total: %s",
            audit["analyzed"],
            audit["skipped"],
            audit["failed"],
            audit["selected"],
        )
        return audit

    finally:
        try:
            if llm_client is not None:
                await llm_client._client.close()
        finally:
            session.close()
