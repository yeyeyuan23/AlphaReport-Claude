"""Collect, classify and persist research papers; optionally analyze the stored rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import logging
from datetime import datetime, timezone
from pathlib import Path


from reportagent.models.schemas import (
    UserCriteria,
    SourceType,
    ClassifiedReport,
    ClassificationResult,
)
from reportagent.sources import factory as source_factory
from reportagent.sources.base import (
    SearchDiagnostics,
    SourceParseError,
    deduplicate_results,
    safe_source_error,
    usable_full_text,
)
from reportagent.classifiers.rule_classifier import RuleClassifier
from reportagent.classifiers.provenance import classification_material, failed_attempt
from reportagent.classifiers.llm_classifier import (
    LLMClassifier,
    ClassificationEvidenceError,
    ClassificationValidationError,
    classification_error_detail,
)
from reportagent.classifiers.quant_filter import is_quant_finance
from reportagent.llm.client import LLMClient
from reportagent.db.engine import init_db, get_session_factory
from reportagent.db.repository import ReportRepository

from reportagent.services.collection_timing import CollectionTiming

logger = logging.getLogger("collect")


@dataclass(frozen=True)
class CollectionOptions:
    topics: str
    sources: str = "arxiv,eastmoney,bigquant"
    keywords: str = ""
    max_results: int = 20
    days: int = 60
    date_from: str | None = None
    date_to: str | None = None
    no_analysis: bool = False
    analysis_depth: str = "standard"
    broad: bool = False


async def classify_for_storage(
    sr,
    classifier,
    llm_classifier,
    errors,
    *,
    model_unavailable: bool = False,
    check_domain: bool = True,
):
    """Apply the shared domain gate and classification fallback before persistence."""
    title = sr.title or ""
    abstract = classification_material(sr)
    error_kind = None
    if not title.strip():
        raise SourceParseError("report missing title")
    if check_domain and not is_quant_finance(title, abstract):
        return None
    if llm_classifier:
        try:
            classification = await llm_classifier.classify(title, abstract)
        except ClassificationEvidenceError as error:
            logger.warning(
                "Classification evidence invalid source=%s arxiv_id=%s title=%r: %s",
                sr.source,
                sr.arxiv_id,
                title[:160],
                classification_error_detail(error),
            )
            errors.append("classification evidence invalid; retry required")
            error_kind = "invalid_evidence"
            classification = ClassificationResult(
                method="unverified", provenance={"status": "unverified"}
            )
        except Exception as error:
            detail = classification_error_detail(error)
            logger.warning(
                "Classification fallback source=%s arxiv_id=%s title=%r: %s",
                sr.source,
                sr.arxiv_id,
                title[:160],
                detail,
            )
            stage = (
                f"classification failed; retry required ({sr.arxiv_id or title[:80]})"
            )
            errors.append(
                f"{stage}: {detail}"
                if isinstance(error, ClassificationValidationError)
                else safe_source_error(error, stage)
            )
            error_kind = "model_error"
            classification = classifier.classify(title, abstract)
    else:
        classification = classifier.classify(title, abstract)
        if model_unavailable:
            error_kind = "model_error"
            errors.append("classification unavailable; retry required")
    if error_kind is not None:
        classification.provenance["last_failed_attempt"] = failed_attempt(
            title, abstract, error_kind
        )
    return ClassifiedReport(search_result=sr, classification=classification)


def build_sources(source_names: list[str]) -> list:
    """Compatibility adapter for explicitly selected collection sources."""
    return source_factory.build_sources(source_names)


def _criteria_from_options(options: CollectionOptions) -> UserCriteria:
    topics = [t.strip() for t in options.topics.split(",") if t.strip()]
    source_names = [s.strip() for s in options.sources.split(",") if s.strip()]
    keywords = [k.strip() for k in options.keywords.split(",") if k.strip()]
    # Date filtering
    date_from = None
    date_to = None
    if options.date_from:
        date_from = datetime.strptime(options.date_from, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    elif options.days:
        from datetime import timedelta

        date_from = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=options.days)
    if options.date_to:
        date_to = datetime.strptime(options.date_to, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
        )

    if date_from:
        logger.info(
            f"Date filter: from {date_from.strftime('%Y-%m-%d')}"
            f" to {date_to.strftime('%Y-%m-%d') if date_to else 'today'}"
        )

    return UserCriteria(
        topics=topics,
        sources=[SourceType(s) for s in source_names],
        keywords=keywords,
        date_from=date_from,
        date_to=date_to,
        max_results_per_source=options.max_results,
    )


async def collect_reports(
    options: CollectionOptions | UserCriteria,
    *,
    no_analysis: bool | None = None,
    analysis_depth: str = "standard",
    broad: bool = False,
    progress_cb: Callable[[str, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """Execute every collection entry through the same selection and save gates.

    Criteria enter intact: comma-bearing list items, timezone-aware instants,
    journals, brokers and local paths are never serialized through CLI strings.
    Cancellation preserves the receipt for metadata already committed.
    """
    collection_timing = CollectionTiming()
    if isinstance(options, CollectionOptions):
        sources = build_sources(
            [name.strip() for name in options.sources.split(",") if name.strip()]
        )
        if not sources:
            raise ValueError("No valid sources specified")
        criteria = _criteria_from_options(options)
        no_analysis = options.no_analysis if no_analysis is None else no_analysis
        analysis_depth = options.analysis_depth
        broad = options.broad
    else:
        criteria = options
        sources = build_sources([name.value for name in criteria.sources])
    topics = criteria.topics
    source_names = [name.value for name in criteria.sources]

    def cancelled():
        return bool(cancel_check and cancel_check())

    def progress(phase, message):
        if progress_cb:
            progress_cb(phase, message)

    init_db()
    classifier = RuleClassifier()
    if not sources and not cancelled():
        raise ValueError("No valid sources specified")

    all_results = []
    storage_result = {
        "total": 0,
        "added": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
        "analyzed": 0,
    }

    # Init LLM-based classification and analysis
    llm_client = None
    llm_classifier = None
    model_unavailable = False
    try:
        llm_client = LLMClient()
        llm_classifier = LLMClassifier(llm_client)
        logger.info("LLM client ready for classification")
    except Exception as e:
        model_unavailable = True
        logger.warning("LLM client init failed: %s", type(e).__name__)

    source_statuses = []
    collection_errors = []
    report_ids = []
    for source in sources:
        if cancelled():
            break
        progress("collecting", f"Searching {source.source_type.value}")
        source_name = source.source_type.value
        source_timing = CollectionTiming(source_name)
        errors = []
        results = []
        valid = 0
        full_text_count = 0
        completed = False
        unavailable = False
        diagnostics = None
        screening = {
            "checked": 0,
            "selected": 0,
            "rejected": 0,
            "pending": 0,
            "failed": 0,
            "cached_scores": 0,
            "superseded": 0,
        }
        online = source.source_type != SourceType.LOCAL_PDF
        classified_results = []
        try:
            available = (
                Path(criteria.local_pdf_path).is_dir()
                if source.source_type == SourceType.LOCAL_PDF
                and criteria.local_pdf_path
                else source.is_available()
            )
            if not available:
                unavailable = True
                errors.append("configuration: source unavailable")
            else:
                logger.info(
                    "Searching %s for: %s%s",
                    source_name,
                    topics,
                    " (BROAD mode)" if broad else "",
                )
                download_pdfs = getattr(source, "download_pdfs", None)
                if online and download_pdfs is not None:
                    source.download_pdfs = False
                try:
                    with source_timing.measure("metadata"):
                        if broad and hasattr(source, "search_broad"):
                            results = await source.search_broad(criteria)
                        else:
                            results = await source.search(criteria)
                finally:
                    if download_pdfs is not None:
                        source.download_pdfs = download_pdfs
                completed = True
                logger.info("  → %d results from %s", len(results), source_name)

                for sr in deduplicate_results(results):
                    if cancelled():
                        break
                    try:
                        if online and criteria.date_basis == "announcement":
                            from reportagent.sources.date_window import (
                                candidate_in_window,
                            )

                            if sr.announced_at is None:
                                errors.append(
                                    "announcement date unavailable; metadata not stored and PDF not downloaded"
                                )
                                continue
                            if not candidate_in_window(sr, criteria):
                                continue
                        if online:
                            from reportagent.services.collection_screening import (
                                reuse_screening_material,
                            )

                            sr = reuse_screening_material(sr, get_session_factory())
                        if cancelled():
                            break
                        if online:
                            from reportagent.services.paper_scoring import (
                                has_screening_material,
                            )

                            if not has_screening_material(sr):
                                screening["checked"] += 1
                                screening["pending"] += 1
                                errors.append(
                                    "screening material unavailable; not rejected as irrelevant and not stored"
                                )
                                continue
                        if online and criteria.date_basis == "announcement":
                            # No lexical/domain gate may hide candidates from the
                            # configured relevance model; classify accepted papers later.
                            classified_results.append(
                                ClassifiedReport(
                                    search_result=sr,
                                    classification=ClassificationResult(
                                        method="unverified"
                                    ),
                                )
                            )
                            continue
                        progress("classifying", f"Classifying {source_name} metadata")
                        with source_timing.measure("classification"):
                            classified = await classify_for_storage(
                                sr,
                                classifier,
                                llm_classifier,
                                errors,
                                model_unavailable=model_unavailable,
                            )
                        if classified is None:
                            screening["checked"] += 1
                            screening["rejected"] += 1
                            continue
                        classified_results.append(classified)
                    except Exception as error:
                        errors.append(safe_source_error(error, "report validation"))
                from reportagent.services.collection_screening import (
                    select_metadata,
                    store_selected,
                    hydrate_selected,
                )

                progress("screening", f"Screening {source_name} metadata")
                with source_timing.measure("relevance"):
                    selected = (
                        []
                        if cancelled()
                        else (
                            await select_metadata(
                                classified_results,
                                llm_client,
                                get_session_factory(),
                                errors,
                                screening,
                            )
                            if online
                            else [(item, None) for item in classified_results]
                        )
                    )
                for item, decision in selected:
                    if cancelled():
                        break
                    progress("storing", f"Saving accepted {source_name} metadata")
                    if cancelled():
                        break
                    try:
                        if online and criteria.date_basis == "announcement":
                            progress(
                                "classifying",
                                f"Classifying accepted {source_name} metadata",
                            )
                            with source_timing.measure("classification"):
                                item = await classify_for_storage(
                                    item.search_result,
                                    classifier,
                                    llm_classifier,
                                    errors,
                                    model_unavailable=model_unavailable,
                                    check_domain=False,
                                )
                            if cancelled():
                                break
                        with source_timing.measure("storage"):
                            if online:
                                action, rid, item = store_selected(
                                    item,
                                    decision,
                                    get_session_factory(),
                                )
                            else:
                                with get_session_factory()() as session:
                                    action, rid = ReportRepository(
                                        session
                                    ).upsert_report(item)
                        storage_result["added" if action == "added" else "updated"] += 1
                        report_ids.append(rid)
                        all_results.append(item)
                        if online and not cancelled():
                            with source_timing.measure("download_parse"):
                                item = await hydrate_selected(
                                    source,
                                    item,
                                    rid,
                                    get_session_factory(),
                                    errors,
                                    cancel_check=cancel_check,
                                )
                            all_results[-1] = item
                        valid += 1
                        sr = item.search_result
                        full_text_count += int(
                            not sr.abstract_only and usable_full_text(sr.full_text)
                        )
                    except Exception as error:
                        errors.append(safe_source_error(error, "report storage"))
        except Exception as error:
            errors.append(safe_source_error(error))

        observed = getattr(source, "last_search", None)
        if isinstance(observed, SearchDiagnostics) and not unavailable:
            diagnostics = observed
            errors = diagnostics.errors + errors
            unavailable = diagnostics.unavailable
        fetched = max(len(results), diagnostics.fetched if diagnostics else 0)
        succeeded = (
            diagnostics.successful_requests > 0 if diagnostics else completed
        ) or valid > 0
        if cancelled():
            status = "cancelled"
        elif unavailable:
            status = "unavailable"
        elif errors:
            status = "partial" if succeeded else "error"
        else:
            status = "ok" if valid else "empty"
        collection_timing.include(source_timing)
        source_clock = source_timing.snapshot()
        source_statuses.append(
            {
                "source": source_name,
                "status": status,
                "fetched": fetched,
                "valid": valid,
                "full_text": full_text_count,
                "elapsed_ms": source_clock["elapsed_ms"],
                "timing": source_clock,
                "errors": errors,
                "screening": screening,
            }
        )
        for error in errors:
            message = f"Source {source_name}: {error}"
            logger.warning(message)
            collection_errors.append(message)
    storage_result.update(total=len(all_results), errors=list(collection_errors))

    # All entrypoints use the batch service's evidence, completeness and cache
    # gates. A source row being stored is not evidence of successful analysis.
    if not no_analysis and report_ids and not cancelled():
        from reportagent.services import batch_analysis
        from reportagent.models.database import Report

        logger.info(
            "Starting shared analysis for %d reports (depth: %s)",
            len(report_ids),
            analysis_depth,
        )
        analysis_results = []
        for rid in report_ids:
            if cancelled():
                break
            progress("analyzing", f"Analyzing report {rid}")
            if cancelled():
                break
            try:
                audit = await batch_analysis.run_analysis(
                    batch_analysis.AnalysisOptions(report_id=rid, depth=analysis_depth),
                    cancel_check=cancel_check,
                )
                storage_result["analyzed"] += audit["analyzed"]
                # Batch error strings may contain upstream request details. Keep
                # public collection observations safe and identify the failed row.
                safe_errors = {
                    str(failed_id): "analysis failed shared validation or model call"
                    for failed_id in audit["failed_ids"]
                }
                if audit["failed_ids"]:
                    storage_result["errors"].append(
                        f"Analysis report {rid}: failed shared validation or model call"
                    )
                if rid in audit["skipped_ids"]:
                    # A skipped valid cache is success; a missing-material skip
                    # remains visible to callers as incomplete collection work.
                    with get_session_factory()() as session:
                        report = session.get(Report, rid)
                        cached = report is not None and batch_analysis._usable_cache(
                            report.analysis_json,
                            batch_analysis.analysis_cache_fingerprint(
                                report, analysis_depth
                            ),
                            analysis_depth,
                        )
                    if not cached:
                        message = (
                            f"Analysis report {rid}: skipped; no usable research text"
                        )
                        storage_result["errors"].append(message)
                        safe_errors[str(rid)] = message
                analysis_results.append({**audit, "errors": safe_errors})
            except Exception as error:
                message = safe_source_error(error, f"analysis report {rid}")
                storage_result["errors"].append(message)
                analysis_results.append(
                    {
                        "report_ids": [rid],
                        "analyzed": 0,
                        "failed_ids": [rid],
                        "errors": {str(rid): message},
                    }
                )
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

    if cancelled():
        status = "cancelled"
    progress(
        "complete" if status == "ok" else status,
        f"Collection {status}: {len(report_ids)} saved reports",
    )
    return {
        "status": status,
        "report_ids": report_ids,
        "source_statuses": source_statuses,
        "topics": topics,
        "sources": source_names,
        "results_count": len(all_results),
        "storage": storage_result,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "timing": collection_timing.snapshot(),
    }
