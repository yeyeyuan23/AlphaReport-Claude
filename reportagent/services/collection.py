"""Collect, classify and persist research papers; optionally analyze the stored rows."""

from __future__ import annotations

from dataclasses import dataclass

import logging
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic


from reportagent.models.schemas import UserCriteria, SourceType, ClassifiedReport
from reportagent.sources.arxiv_source import ArxivSource
from reportagent.sources.eastmoney_source import EastMoneySource
from reportagent.sources.bigquant_source import BigQuantSource
from reportagent.sources.local_pdf import LocalPDFSource
from reportagent.sources.base import (
    SearchDiagnostics, SourceParseError, deduplicate_results, safe_source_error, usable_full_text,
)
from reportagent.classifiers.rule_classifier import RuleClassifier
from reportagent.classifiers.llm_classifier import LLMClassifier
from reportagent.classifiers.quant_filter import is_quant_finance
from reportagent.llm.client import LLMClient
from reportagent.db.engine import init_db, get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.utils.config import get_config

logger = logging.getLogger("collect")


@dataclass(frozen=True)
class CollectionOptions:
    topics: str
    sources: str = 'arxiv,eastmoney,bigquant'
    keywords: str = ''
    max_results: int = 20
    days: int = 60
    date_from: str | None = None
    date_to: str | None = None
    no_analysis: bool = False
    analysis_depth: str = 'standard'
    broad: bool = False


def build_sources(source_names: list[str]) -> list:
    sources = []
    for name in source_names:
        if name == "arxiv":
            sources.append(ArxivSource())
        elif name == "eastmoney":
            sources.append(EastMoneySource())
        elif name == "bigquant":
            sources.append(BigQuantSource())
        elif name == "local_pdf":
            lib_path = get_config("sources", "local_pdf", "library_path", default="data/pdf_library")
            sources.append(LocalPDFSource(str(Path(lib_path))))
    return sources


async def collect_reports(options: CollectionOptions) -> dict:
    topics = [t.strip() for t in options.topics.split(",") if t.strip()]
    source_names = [s.strip() for s in options.sources.split(",") if s.strip()]
    keywords = [k.strip() for k in options.keywords.split(",") if k.strip()]

    # Date filtering
    date_from = None
    date_to = None
    if options.date_from:
        date_from = datetime.strptime(options.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif options.days:
        from datetime import timedelta
        date_from = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=options.days)
    if options.date_to:
        date_to = datetime.strptime(options.date_to, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc)

    if date_from:
        logger.info(f"Date filter: from {date_from.strftime('%Y-%m-%d')}"
                    f" to {date_to.strftime('%Y-%m-%d') if date_to else 'today'}")

    # Init DB
    init_db()
    classifier = RuleClassifier()

    # Build sources
    sources = build_sources(source_names)
    if not sources:
        logger.error("No valid sources specified")
        raise ValueError("No valid sources specified")

    criteria = UserCriteria(
        topics=topics,
        sources=[SourceType(s) for s in source_names],
        keywords=keywords,
        date_from=date_from,
        date_to=date_to,
        max_results_per_source=options.max_results,
    )

    all_results = []
    storage_result = {"total": 0, "added": 0, "updated": 0, "skipped": 0, "errors": [],
                      "analyzed": 0}

    # Init LLM-based classification and analysis
    llm_client = None
    llm_classifier = None
    try:
        llm_client = LLMClient()
        llm_classifier = LLMClassifier(llm_client)
        logger.info("LLM client ready for classification")
    except Exception as e:
        logger.warning(f"LLM client init failed: {e}, falling back to rule-based only")

    source_statuses = []
    collection_errors = []
    for source in sources:
        started = monotonic()
        source_name = source.source_type.value
        errors = []
        results = []
        valid = 0
        full_text_count = 0
        completed = False
        unavailable = False
        diagnostics = None
        try:
            if not source.is_available():
                unavailable = True
                errors.append("configuration: source unavailable")
            else:
                logger.info("Searching %s for: %s%s", source_name, topics,
                            " (BROAD mode)" if options.broad else "")
                if options.broad and hasattr(source, "search_broad"):
                    results = await source.search_broad(criteria)
                else:
                    results = await source.search(criteria)
                completed = True
                logger.info("  → %d results from %s", len(results), source_name)

                for sr in deduplicate_results(results):
                    try:
                        title = sr.title or ""
                        abstract = sr.abstract or ""
                        if not title.strip():
                            raise SourceParseError("report missing title")
                        if not is_quant_finance(title, abstract):
                            continue

                        if llm_classifier:
                            try:
                                classification = await llm_classifier.classify(title, abstract)
                            except Exception as error:
                                logger.warning("Classification fallback: %s", type(error).__name__)
                                errors.append(safe_source_error(error, "classification; rule fallback used"))
                                classification = classifier.classify(title, abstract)
                        else:
                            classification = classifier.classify(title, abstract)

                        all_results.append(ClassifiedReport(search_result=sr, classification=classification))
                        valid += 1
                        full_text_count += int(not sr.abstract_only and usable_full_text(sr.full_text))
                    except Exception as error:
                        errors.append(safe_source_error(error, "report validation"))
        except Exception as error:
            errors.append(safe_source_error(error))

        observed = getattr(source, "last_search", None)
        if isinstance(observed, SearchDiagnostics) and not unavailable:
            diagnostics = observed
            errors = diagnostics.errors + errors
            unavailable = diagnostics.unavailable
        fetched = max(len(results), diagnostics.fetched if diagnostics else 0)
        succeeded = (diagnostics.successful_requests > 0 if diagnostics else completed) or valid > 0
        if unavailable:
            status = "unavailable"
        elif errors:
            status = "partial" if succeeded else "error"
        else:
            status = "ok" if valid else "empty"
        source_statuses.append({
            "source": source_name, "status": status,
            "fetched": fetched, "valid": valid, "full_text": full_text_count,
            "elapsed_ms": round((monotonic() - started) * 1000), "errors": errors,
        })
        for error in errors:
            message = f"Source {source_name}: {error}"
            logger.warning(message)
            collection_errors.append(message)
    storage_result["errors"] = list(collection_errors)

    # Step 3: Store to DB
    report_ids = []
    if all_results:
        factory = get_session_factory()
        session = factory()
        try:
            repo = ReportRepository(session)
            result = repo.batch_upsert(all_results)
            report_ids = result.report_ids
            storage_result = {
                "total": result.total_processed,
                "added": result.newly_added,
                "updated": result.updated,
                "skipped": result.duplicate_skipped,
                "errors": collection_errors + result.errors,
                "analyzed": 0,
            }
            logger.info(f"Storage: {result.newly_added} added, {result.updated} updated, "
                        f"{result.duplicate_skipped} skipped")
        finally:
            session.close()

    # All entrypoints use the batch service's evidence, completeness and cache
    # gates. A source row being stored is not evidence of successful analysis.
    if not options.no_analysis and report_ids:
        from reportagent.services import batch_analysis
        from reportagent.models.database import Report

        logger.info("Starting shared analysis for %d reports (depth: %s)",
                    len(report_ids), options.analysis_depth)
        analysis_results = []
        for rid in report_ids:
            try:
                audit = await batch_analysis.run_analysis(batch_analysis.AnalysisOptions(
                    report_id=rid, depth=options.analysis_depth))
                storage_result["analyzed"] += audit["analyzed"]
                # Batch error strings may contain upstream request details. Keep
                # public collection observations safe and identify the failed row.
                safe_errors = {str(failed_id): "analysis failed shared validation or model call"
                               for failed_id in audit["failed_ids"]}
                if audit["failed_ids"]:
                    storage_result["errors"].append(f"Analysis report {rid}: failed shared validation or model call")
                if rid in audit["skipped_ids"]:
                    # A skipped valid cache is success; a missing-material skip
                    # remains visible to callers as incomplete collection work.
                    with get_session_factory()() as session:
                        report = session.get(Report, rid)
                        cached = report is not None and batch_analysis._usable_cache(
                            report.analysis_json,
                            batch_analysis.analysis_cache_fingerprint(report, options.analysis_depth),
                            options.analysis_depth)
                    if not cached:
                        message = f"Analysis report {rid}: skipped; no usable research text"
                        storage_result["errors"].append(message)
                        safe_errors[str(rid)] = message
                analysis_results.append({**audit, "errors": safe_errors})
            except Exception as error:
                message = safe_source_error(error, f"analysis report {rid}")
                storage_result["errors"].append(message)
                analysis_results.append({"report_ids": [rid], "analyzed": 0,
                                         "failed_ids": [rid], "errors": {str(rid): message}})
        storage_result["analysis_results"] = analysis_results

    source_states = {item["status"] for item in source_statuses}
    if source_states <= {"ok", "empty"}:
        status = "ok"
    elif source_states & {"ok", "empty", "partial"}:
        status = "partial"
    else:
        status = "failed"
    # Storage errors are failures even if every source request succeeded.
    if len(storage_result["errors"]) > len(collection_errors):
        status = "partial" if report_ids else "failed"

    return {
        "status": status,
        "source_statuses": source_statuses,
        "topics": topics,
        "sources": source_names,
        "results_count": len(all_results),
        "storage": storage_result,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
