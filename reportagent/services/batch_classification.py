"""Reclassify stored reports with rules, a model, or both."""

from __future__ import annotations

from dataclasses import dataclass

import asyncio
import logging


from reportagent.db.engine import init_db, get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.models.database import Report
from reportagent.classifiers.rule_classifier import RuleClassifier
from reportagent.classifiers.llm_classifier import LLMClassifier
from reportagent.llm.client import LLMClient
from reportagent.models.schemas import (
    ClassificationResult,
    Market,
    AssetClass,
    Frequency,
    Topic,
)

logger = logging.getLogger("classify")

_ENUM_MAP = {
    "markets": Market,
    "asset_classes": AssetClass,
    "frequencies": Frequency,
    "topics": Topic,
}


@dataclass(frozen=True)
class ClassificationOptions:
    all: bool = False
    report_id: int | None = None
    recent: int | None = None
    llm: bool = False
    hybrid: bool = False
    dry_run: bool = False


def _merge_results(rule: ClassificationResult, llm: ClassificationResult) -> ClassificationResult:
    """Model evidence is authoritative; rule guesses cannot restore rejected labels."""
    return llm.model_copy(update={"method": "hybrid"})


def _summary(rep: Report) -> str:
    parts = []
    if rep.markets:
        parts.append(f"市场:[{rep.markets}]")
    if rep.asset_classes:
        parts.append(f"资产:[{rep.asset_classes}]")
    if rep.frequencies:
        parts.append(f"频率:[{rep.frequencies}]")
    if rep.topics:
        parts.append(f"主题:[{rep.topics}]")
    tags = [t.value for t in rep.tags if t.dimension == "custom"]
    if tags:
        parts.append(f"标签:{tags}")
    return " ".join(parts) if parts else "(无标签)"


def _validate_selection(options: ClassificationOptions) -> None:
    """Reject ambiguous or unbounded selections before opening any resources."""
    if type(options.all) is not bool:
        raise ValueError("all must be a boolean")
    for name in ("report_id", "recent"):
        value = getattr(options, name)
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(f"{name} must be a positive integer")
    if sum((options.all, options.report_id is not None, options.recent is not None)) != 1:
        raise ValueError("Select exactly one of all, report_id, or recent")


async def classify_async(options: ClassificationOptions) -> dict:
    _validate_selection(options)
    init_db()
    factory = get_session_factory()
    session = factory()

    rule_classifier = RuleClassifier()
    llm_classifier = None
    llm_client = None

    if options.llm or options.hybrid:
        llm_client = LLMClient()
        llm_classifier = LLMClassifier(llm_client)

    try:
        repo = ReportRepository(session)

        if options.report_id is not None:
            reports = [repo.get_report(options.report_id)]
            if not reports[0]:
                logger.error(f"Report {options.report_id} not found")
                raise ValueError("Report not found")
        elif options.recent is not None:
            reports = session.query(Report).order_by(Report.created_at.desc(), Report.id.desc()).limit(options.recent).all()
        else:
            reports = session.query(Report).order_by(Report.id).all()

        logger.info(f"Processing {len(reports)} reports "
                    f"(method: {'hybrid' if options.hybrid else 'llm' if options.llm else 'rule'})...")

        audit = {"selected": len(reports), "classified": 0, "failed": 0, "updated": 0,
                 "report_ids": [r.id for r in reports], "classified_ids": [], "failed_ids": [], "errors": {}}
        updated = 0
        for i, r in enumerate(reports):
            title = r.title or ""
            abstract = r.abstract or ""

            try:
                if options.hybrid:
                    rule_result = rule_classifier.classify(title, abstract)
                    llm_result = await llm_classifier.classify(title, abstract)
                    result = _merge_results(rule_result, llm_result)
                elif options.llm:
                    result = await llm_classifier.classify(title, abstract)
                else:
                    result = rule_classifier.classify(title, abstract)

            except Exception as error:
                audit["failed_ids"].append(r.id)
                audit["errors"][r.id] = str(error)[:500]
                logger.error("Classification failed for paper %s; existing tags retained: %s", r.id, error)
                continue
            audit["classified_ids"].append(r.id)

            if repo.apply_classification(r, result, dry_run=options.dry_run):
                updated += 1
                summary = _summary(r)
                logger.info(f"  [{i+1}/{len(reports)}] {r.title[:50]} → {summary}")

            # Rate limit for LLM: small delay between calls
            if (options.llm or options.hybrid) and i < len(reports) - 1:
                await asyncio.sleep(0.3)

        if not options.dry_run and updated > 0:
            session.commit()
            logger.info(f"✅ Updated {updated}/{len(reports)} reports")
        elif options.dry_run:
            logger.info(f"[DRY RUN] Would update {updated}/{len(reports)} reports")
        else:
            logger.info("No changes needed")
        audit.update(classified=len(audit["classified_ids"]), failed=len(audit["failed_ids"]), updated=updated)
        return audit

    finally:
        try:
            if llm_client is not None:
                await llm_client._client.close()
        finally:
            session.close()
