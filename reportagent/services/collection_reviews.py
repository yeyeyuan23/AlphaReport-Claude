"""Prepare saved full-text evidence as the last scheduled collection phase."""

import json
from reportagent.db.engine import get_session_factory
from reportagent.utils.config import get_llm_model
from reportagent.services.report_quality import (
    cached_review_is_current,
    has_review_material,
    resolve_review_budget,
)


def _review_candidates(force_review):
    from reportagent.services.paper_scoring import (
        screening_is_current,
        _load_scoring_config,
        _configured_threshold,
    )
    from reportagent.models.database import Report

    with get_session_factory()() as session:
        rows = (
            session.query(Report)
            .filter(
                Report.published_date.isnot(None),
                Report.source.in_(["arxiv", "eastmoney", "bigquant"]),
            )
            .all()
        )
        threshold = _configured_threshold(_load_scoring_config())
        candidates, cached_ids, skipped_ids = [], [], []
        for row in rows:
            if (
                not screening_is_current(row)
                or row.selection_relevance is None
                or row.selection_relevance < threshold
            ):
                continue
            if not has_review_material(row):
                skipped_ids.append(row.id)
                continue
            snapshot = dict(vars(row))
            try:
                cached = json.loads(row.evaluation_json or "null")
            except (ValueError, TypeError):
                cached = None
            if not force_review and cached_review_is_current(
                snapshot, cached, get_llm_model()
            ):
                cached_ids.append(row.id)
                continue  # Saved reviews remain visible without consuming new-review budget.
            candidates.append(snapshot)
    return candidates, cached_ids, skipped_ids


async def prepare_collection_reviews(
    *, max_reviews: int | None = None, force_review: bool = False
) -> dict:
    """Review accepted online papers and retry unfinished work on later collections."""
    from reportagent.services.report_quality import review_report_ids, select_candidates

    max_reviews = resolve_review_budget(max_reviews)
    audit = {
        "review_ids": [],
        "failures": [],
    }
    candidates, cached_ids, skipped_ids = _review_candidates(force_review)
    audit["skipped_material_ids"] = skipped_ids
    audit["unreviewed_candidates"] = len(candidates)
    selected = select_candidates(candidates, max_reviews=len(candidates))
    review_audit = {}
    reviewed = (
        await review_report_ids(
            [p["id"] for p in selected],
            force=force_review,
            max_reviews=max_reviews,
            audit=review_audit,
        )
        if selected
        else {}
    )
    audit.update(review_audit)
    audit["cached_ids"] = list(dict.fromkeys(cached_ids + audit.get("cached_ids", [])))
    audit["review_ids"] = review_audit.get("review_ids", list(reviewed))
    audit["completed_ids"] = [
        rid
        for rid, result in reviewed.items()
        if result.get("status") == "reviewed" and rid not in audit["cached_ids"]
    ]
    audit["review_tiers"] = {
        str(rid): result.get("tier") for rid, result in reviewed.items()
    }
    for rid, result in reviewed.items():
        if result.get("status") == "skipped":
            audit["skipped_material_ids"].append(rid)
        elif result.get("status") != "reviewed":
            audit["failures"].append(
                {
                    "stage": "review",
                    "id": rid,
                    "reason": result.get("validation_issues", []),
                }
            )
    return audit
