"""Reclassify stored reports with rules, a model, or both."""

from __future__ import annotations

from dataclasses import dataclass

import asyncio
import logging


from reportagent.db.engine import init_db, get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.models.database import Report, Tag
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
    """Merge rule and LLM results, taking union of tags."""
    return ClassificationResult(
        markets=list(set(rule.markets) | set(llm.markets)),
        asset_classes=list(set(rule.asset_classes) | set(llm.asset_classes)),
        frequencies=list(set(rule.frequencies) | set(llm.frequencies)),
        topics=list(set(rule.topics) | set(llm.topics)),
        custom_tags=list(set(rule.custom_tags) | set(llm.custom_tags)),
        confidence=max(rule.confidence, llm.confidence),
        method="hybrid",
    )


def _ensure_tags(session, db_report: Report, custom_tags: list[str]) -> bool:
    """Sync custom tags. Returns True if any changes made."""
    dimension = "custom"
    existing = {(t.dimension, t.value) for t in db_report.tags}

    changed = False
    for value in custom_tags:
        if (dimension, value) not in existing:
            tag = session.query(Tag).filter(
                Tag.dimension == dimension, Tag.value == value
            ).first()
            if not tag:
                tag = Tag(dimension=dimension, value=value)
                session.add(tag)
                session.flush()
            if tag not in db_report.tags:
                db_report.tags.append(tag)
                changed = True

    return changed


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


async def classify_async(options: ClassificationOptions) -> None:
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

        if options.report_id:
            reports = [repo.get_report(options.report_id)]
            if not reports[0]:
                logger.error(f"Report {options.report_id} not found")
                raise ValueError("Report not found")
        elif options.recent:
            reports = session.query(Report).order_by(Report.created_at.desc(), Report.id.desc()).limit(options.recent).all()
        else:
            reports = session.query(Report).order_by(Report.id).all()

        logger.info(f"Processing {len(reports)} reports "
                    f"(method: {'hybrid' if options.hybrid else 'llm' if options.llm else 'rule'})...")

        updated = 0
        for i, r in enumerate(reports):
            title = r.title or ""
            abstract = r.abstract or ""

            if options.hybrid:
                rule_result = rule_classifier.classify(title, abstract)
                llm_result = await llm_classifier.classify(title, abstract)
                result = _merge_results(rule_result, llm_result)
            elif options.llm:
                result = await llm_classifier.classify(title, abstract)
            else:
                result = rule_classifier.classify(title, abstract)

            new_markets = ",".join(m.value for m in result.markets)
            new_assets = ",".join(a.value for a in result.asset_classes)
            new_freqs = ",".join(f.value for f in result.frequencies)
            new_topics = ",".join(t.value for t in result.topics)
            new_custom = result.custom_tags or []

            tags_changed = _ensure_tags(session, r, new_custom)
            fields_changed = (
                new_markets != (r.markets or "") or
                new_assets != (r.asset_classes or "") or
                new_freqs != (r.frequencies or "") or
                new_topics != (r.topics or "")
            )

            if fields_changed or tags_changed:
                if not options.dry_run:
                    r.markets = new_markets
                    r.asset_classes = new_assets
                    r.frequencies = new_freqs
                    r.topics = new_topics
                    r.classification_confidence = result.confidence
                    r.classification_method = result.method
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

    finally:
        try:
            if llm_client is not None:
                await llm_client._client.close()
        finally:
            session.close()
