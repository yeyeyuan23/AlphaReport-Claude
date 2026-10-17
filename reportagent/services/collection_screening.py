"""Screen online metadata in memory; rejected papers never reach persistence."""

from __future__ import annotations

import json
import math
import re

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Report
from reportagent.services import paper_scoring as scoring
from reportagent.sources.base import (
    SourceParseError,
    safe_source_error,
    usable_full_text,
)
from reportagent.processors.paper_sections import extract_paper_sections


def snapshot(classified):
    paper = classified.search_result.model_dump(mode="json")
    paper["topics"] = ",".join(
        topic.value for topic in classified.classification.topics
    )
    return paper


def same_metadata(paper, row):
    return all(
        paper.get(key) == getattr(row, key) for key in ("title", "abstract", "arxiv_id")
    )


def stored_snapshot(row):
    if row is None:
        return None
    fields = (
        "id",
        "title",
        "abstract",
        "arxiv_id",
        "source_url",
        "announced_at",
        "announcement_info_json",
        "source_item_id",
        "doi",
        "full_text",
        "has_full_text",
        "pdf_path",
        "tables_json",
        "equations_json",
        "classification_provenance_json",
        "topics",
        "markets",
        "asset_classes",
        "frequencies",
        "selection_provenance_json",
        "selection_relevance",
        "selection_direction",
        "selection_reason",
    )
    return {key: getattr(row, key) for key in fields}


def superseded(result, existing):
    if existing is None or not result.arxiv_id or not existing.arxiv_id:
        return False
    if re.sub(r"v\d+$", "", result.arxiv_id) != re.sub(r"v\d+$", "", existing.arxiv_id):
        return False
    old = re.search(r"v(\d+)$", result.arxiv_id)
    current = re.search(r"v(\d+)$", existing.arxiv_id)
    return bool(current and (old is None or int(current[1]) > int(old[1])))


def reuse_screening_material(result, factory):
    """Fill missing abstracts from the library without downloading unscreened PDFs."""
    if scoring.has_screening_material(result):
        return result
    with factory() as session:
        existing = ReportRepository(session).find_report(result)
        if (
            existing is not None
            and existing.title == result.title
            and existing.arxiv_id == result.arxiv_id
        ):
            abstract = existing.abstract
            if not abstract and existing.full_text:
                abstract = extract_paper_sections(existing.full_text)["abstract"]
            if scoring.has_screening_material(
                {"title": result.title, "abstract": abstract}
            ):
                return result.model_copy(
                    update={
                        "abstract": abstract,
                        "full_text": existing.full_text,
                        "abstract_only": not existing.has_full_text,
                        "pdf_path": existing.pdf_path,
                        "tables_json": existing.tables_json,
                        "equations_json": existing.equations_json,
                    }
                )
    return result


async def select_metadata(classified, llm, factory, errors, stats):
    """Reuse current decisions or batch-score without inserting candidate rows."""
    decisions, pending, observed = {}, [], {}
    with factory() as session:
        repo = ReportRepository(session)
        for index, item in enumerate(classified):
            paper = snapshot(item)
            existing = repo.find_report(item.search_result)
            observed[index] = stored_snapshot(existing)
            if superseded(item.search_result, existing):
                decisions[index] = {"status": "superseded"}
                continue
            if (
                existing is not None
                and same_metadata(paper, existing)
                and scoring.screening_is_current(existing)
            ):
                provenance = json.loads(existing.selection_provenance_json)
                value = existing.selection_relevance
                if (
                    provenance.get("status") == "scored"
                    and type(value) in (float, int)
                    and math.isfinite(value)
                    and 0 <= value <= 10
                ):
                    decisions[index] = {
                        "status": "scored",
                        "relevance": value,
                        "best_direction": existing.selection_direction,
                        "reason": existing.selection_reason,
                        "_paper": paper,
                        "_provenance": provenance,
                    }
                    stats["cached_scores"] += 1
                    continue
            pending.append((index, paper))
    if pending:
        if llm is None:
            errors.append(
                "relevance screening unavailable; metadata not stored and PDFs not downloaded"
            )
        else:
            results, failures = await scoring.score_papers_batch(
                llm, [paper for _, paper in pending]
            )
            positions = {id(paper): index for index, paper in pending}
            for result in results:
                index = positions.get(id(result.get("_paper")))
                if index is not None:
                    decisions[index] = result
            if failures:
                errors.append(
                    "relevance screening incomplete; unscored metadata not stored"
                )
    selected = []
    # Scoring may have observed a newer policy than the start of this batch.
    threshold = scoring._configured_threshold(scoring._load_scoring_config())
    for index, item in enumerate(classified):
        stats["checked"] += 1
        decision = decisions.get(index)
        if decision is None:
            stats["failed"] += 1
            continue
        if decision.get("status") == "superseded":
            stats["superseded"] += 1
            continue
        paper = snapshot(item)
        provenance = decision.get("_provenance") or {}
        if provenance.get(
            "fingerprint"
        ) != scoring.scoring_fingerprint() or provenance.get(
            "evidence_fingerprint"
        ) != scoring.evidence_fingerprint(paper):
            errors.append(
                "relevance policy or metadata changed during screening; decision discarded"
            )
            stats["failed"] += 1
        elif decision.get("status") != "scored":
            stats["pending"] += 1
            errors.append(
                "relevance screening needs usable abstract; metadata not stored"
            )
        elif decision["relevance"] < threshold:
            stats["rejected"] += 1
        else:
            stats["selected"] += 1
            decision["_stored_snapshot"] = observed[index]
            selected.append((item, decision))
    return selected


def store_selected(item, decision, factory):
    """Only accepted metadata enters the library; bind its score to that evidence."""
    with factory() as session:
        if session.bind.dialect.name == "sqlite":
            # Serialize only the brief persistence section, never network/model work.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        repo = ReportRepository(session)
        current = repo.find_report(item.search_result)
        if stored_snapshot(current) != decision["_stored_snapshot"]:
            raise SourceParseError(
                "library metadata changed during screening; decision not stored"
            )
        if decision["_provenance"]["fingerprint"] != scoring.scoring_fingerprint():
            raise SourceParseError("relevance policy changed before storage")
        if decision["relevance"] < scoring._configured_threshold(
            scoring._load_scoring_config()
        ):
            raise SourceParseError(
                "relevance below current threshold; decision not stored"
            )
        action, rid = repo.upsert_report(item, commit=False)
        row = session.get(Report, rid)
        if not same_metadata(snapshot(item), row):
            raise SourceParseError(
                "newer library revision rejected the screened metadata"
            )
        provenance = decision["_provenance"]
        if provenance["fingerprint"] != scoring.scoring_fingerprint():
            raise SourceParseError("relevance policy changed before storage")
        repo.update_selection(
            rid,
            decision["relevance"],
            decision["reason"],
            True,
            direction=decision["best_direction"],
            relevance=decision["relevance"],
            provenance_json=json.dumps(provenance, ensure_ascii=False),
            commit=False,
        )
        session.commit()
        return action, rid, item


async def hydrate_selected(source, item, rid, factory, errors, *, cancel_check=None):
    """Reuse usable stored material; never replace concurrent newer metadata."""
    with factory() as session:
        row = session.get(Report, rid)
        before = stored_snapshot(row)
        if not same_metadata(snapshot(item), row):
            return item
        if row.has_full_text and usable_full_text(row.full_text):
            return item.model_copy(
                update={
                    "search_result": item.search_result.model_copy(
                        update={
                            "full_text": row.full_text,
                            "abstract_only": False,
                            "pdf_path": row.pdf_path,
                            "tables_json": row.tables_json,
                            "equations_json": row.equations_json,
                        }
                    )
                }
            )
    if not item.search_result.abstract_only and usable_full_text(
        item.search_result.full_text
    ):
        return item
    hydrate = getattr(source, "hydrate_result", None)
    if hydrate is None:
        return item
    try:
        hydrated = await hydrate(item.search_result)
        if hydrated is None:
            raise SourceParseError("selected paper no longer available")
        # Downloading must not silently replace the evidence already screened.
        if any(
            getattr(hydrated, key) != getattr(item.search_result, key)
            for key in ("title", "abstract", "arxiv_id")
        ):
            raise SourceParseError(
                "selected paper metadata changed during download; retry screening"
            )
        enriched = item.model_copy(update={"search_result": hydrated})
        if cancel_check and cancel_check():
            return item
        with factory() as session:
            if session.bind.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.get(Report, rid)
            if stored_snapshot(row) != before:
                raise SourceParseError(
                    "library material changed during download; downloaded result not applied"
                )
            ReportRepository(session).upsert_report(enriched)
        return enriched
    except Exception as error:
        errors.append(safe_source_error(error, "selected paper hydration"))
        return item
