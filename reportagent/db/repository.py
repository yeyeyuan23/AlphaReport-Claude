from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import or_, desc, asc, func
from sqlalchemy.orm import Session

from reportagent.models.database import Report, Tag, SearchHistory
from reportagent.models.schemas import (
    ClassifiedReport,
    StorageResult,
    ReportListParams,
    SourceType,
)
from reportagent.utils.hashing import content_hash
from reportagent.db.report_identity import identities, identity_hash, compatible_match


class ReportRepository:
    def __init__(self, session: Session):
        self.session = session

    def find_report(self, sr):
        """Resolve strong identifiers first, including rows written by older versions."""
        keys = identities(sr)
        filters = [Report.content_hash == identity_hash(sr)]
        if keys["source_id"]:
            filters.insert(0, Report.source_item_id == keys["source_id"])
        if keys["doi"]:
            # Prefix/case-normalization is checked exactly in Python below.
            filters.append(
                func.lower(Report.doi).contains(keys["doi"], autoescape=True)
            )
        if keys["arxiv"]:
            filters.append(
                or_(
                    Report.arxiv_id == keys["arxiv"],
                    Report.arxiv_id.like(keys["arxiv"] + "v%"),
                )
            )
        if keys["url"]:
            filters.append(
                func.lower(Report.source_url).like(
                    keys["url"].lower().split("#", 1)[0] + "%"
                )
            )
        filters.append(Report.content_hash == content_hash(sr.title, sr.authors))
        candidates = (
            self.session.query(Report).filter(or_(*filters)).order_by(Report.id).all()
        )
        # Strong identifiers outrank URL matches when legacy duplicates coexist.
        candidates.sort(
            key=lambda row: (
                not bool(keys["doi"] and identities(row)["doi"] == keys["doi"]),
                not bool(keys["arxiv"] and identities(row)["arxiv"] == keys["arxiv"]),
                not bool(
                    keys["source_id"]
                    and identities(row)["source_id"] == keys["source_id"]
                ),
            )
        )
        return next((row for row in candidates if compatible_match(sr, row)), None)

    def upsert_report(
        self, report: ClassifiedReport, *, commit: bool = True
    ) -> tuple[str, int]:
        sr = report.search_result
        cl = report.classification

        c_hash = identity_hash(sr)
        existing = self.find_report(sr)

        markets_str = ",".join(m.value for m in cl.markets)
        asset_classes_str = ",".join(a.value for a in cl.asset_classes)
        frequencies_str = ",".join(f.value for f in cl.frequencies)
        topics_str = ",".join(t.value for t in cl.topics)

        if existing:
            old_version = re.search(r"v(\d+)$", existing.arxiv_id or "")
            new_version = re.search(r"v(\d+)$", sr.arxiv_id or "")
            if old_version and (
                not new_version or int(new_version[1]) < int(old_version[1])
            ):
                # A delayed source response must not roll the library back.
                return "updated", existing.id
            revision_changed = bool(sr.arxiv_id and sr.arxiv_id != existing.arxiv_id)
            checked_local_pdf = (
                sr.source == SourceType.LOCAL_PDF and existing.source == "local_pdf"
            )
            checked_abstract = (
                checked_local_pdf and "abstract_source" in sr.raw_metadata
            )
            replace_material = revision_changed or bool(
                sr.full_text and not sr.abstract_only
            )
            material_changed = (
                replace_material
                and (
                    any(
                        getattr(existing, key) != getattr(sr, key)
                        for key in (
                            "full_text",
                            "pdf_path",
                            "tables_json",
                            "equations_json",
                        )
                    )
                    or existing.has_full_text
                    != (bool(sr.full_text) and not sr.abstract_only)
                )
            ) or bool(
                sr.full_text
                and not existing.has_full_text
                and sr.full_text != existing.full_text
            )
            screening_changed = (
                revision_changed
                or sr.title != existing.title
                or bool(
                    (sr.abstract or checked_abstract)
                    and sr.abstract != existing.abstract
                )
            )
            # Relevance is based on title/abstract evidence; obtaining or reparsing
            # the same revision's PDF only invalidates full-text-derived outputs.
            if screening_changed or material_changed:
                existing.analysis_json = existing.evaluation_json = (
                    existing.knowledge_card_json
                ) = existing.summary = None
            if screening_changed:
                existing.selection_provenance_json = existing.selection_direction = (
                    existing.selection_relevance
                ) = None
                existing.selection_score = 0.0
                existing.selection_reason = None
                existing.is_selected = False
            existing.title = sr.title
            existing.authors = json.dumps(sr.authors, ensure_ascii=False)
            source_item_id = identities(sr)["source_id"]
            if source_item_id and (
                not existing.source_item_id
                or source_item_id.split(":", 1)[0]
                == existing.source_item_id.split(":", 1)[0]
            ):
                existing.source_item_id = source_item_id
            if sr.doi:
                existing.doi = sr.doi
            if sr.source_url and (replace_material or not existing.source_url):
                existing.source_url = sr.source_url
            if sr.arxiv_id:
                existing.arxiv_id = sr.arxiv_id
            # Publication dates remain stable across reposts and revisions.
            # Re-import only fills a previously unknown date; it never replaces
            # a known date with a later page/revision timestamp or a missing value.
            if checked_local_pdf and sr.raw_metadata.get("publication_date_checked"):
                # Legacy local imports incorrectly used file creation timestamps.
                # A fresh explicit-date check can repair those values to unknown.
                existing.published_date = sr.published_date
                existing.publication_date_info_json = json.dumps(
                    sr.raw_metadata.get("publication_date_info", {}), ensure_ascii=False
                )
            elif sr.published_date is not None and existing.published_date is None:
                existing.published_date = sr.published_date
            if replace_material:
                existing.full_text = sr.full_text
                existing.has_full_text = bool(sr.full_text) and not sr.abstract_only
                existing.pdf_path = sr.pdf_path
                existing.tables_json = sr.tables_json
                existing.equations_json = sr.equations_json
            elif sr.full_text and not existing.has_full_text:
                existing.full_text = sr.full_text
                existing.has_full_text = not sr.abstract_only
            if sr.abstract or checked_abstract:
                existing.abstract = sr.abstract
            self.apply_classification(existing, cl)
            if commit:
                self.session.commit()
            else:
                self.session.flush()
            return "updated", existing.id

        db_report = Report(
            title=sr.title,
            authors=json.dumps(sr.authors),
            abstract=sr.abstract,
            full_text=sr.full_text,
            has_full_text=bool(sr.full_text) and not sr.abstract_only,
            source=sr.source.value,
            source_url=sr.source_url,
            source_item_id=identities(sr)["source_id"] or None,
            doi=sr.doi,
            arxiv_id=sr.arxiv_id,
            published_date=sr.published_date,
            publication_date_info_json=json.dumps(
                sr.raw_metadata.get("publication_date_info"), ensure_ascii=False
            )
            if sr.raw_metadata.get("publication_date_info")
            else None,
            pdf_path=sr.pdf_path,
            content_hash=c_hash,
            tables_json=sr.tables_json,
            equations_json=sr.equations_json,
            markets=markets_str,
            asset_classes=asset_classes_str,
            frequencies=frequencies_str,
            topics=topics_str,
            classification_confidence=cl.confidence,
            classification_method=cl.method,
        )
        self.session.add(db_report)
        self.apply_classification(db_report, cl)
        if commit:
            self.session.commit()
        else:
            self.session.flush()

        return "added", db_report.id

    def apply_classification(
        self, db_report: Report, cl, *, dry_run: bool = False
    ) -> bool:
        """Replace classification columns, evidence and owned tag links atomically.

        Empty dimensions are meaningful: they remove previous guesses. Shared
        Tag rows and non-classification associations remain available to others.
        The caller owns the transaction.
        """
        fields = {
            name: ",".join(value.value for value in getattr(cl, name))
            for name in ("markets", "asset_classes", "frequencies", "topics")
        }
        fields.update(
            classification_confidence=cl.confidence,
            classification_method=cl.method,
            classification_provenance_json=json.dumps(
                cl.provenance, ensure_ascii=False, sort_keys=True
            )
            if cl.provenance
            else None,
        )
        tag_pairs = []
        for m in cl.markets:
            tag_pairs.append(("market", m.value))
        for a in cl.asset_classes:
            tag_pairs.append(("asset_class", a.value))
        for f in cl.frequencies:
            tag_pairs.append(("frequency", f.value))
        for t in cl.topics:
            tag_pairs.append(("topic", t.value))
        for c in cl.custom_tags:
            tag_pairs.append(("custom", c))
        owned = {"market", "asset_class", "frequency", "topic", "custom"}
        current = {
            (tag.dimension, tag.value)
            for tag in db_report.tags
            if tag.dimension in owned
        }
        changed = current != set(tag_pairs) or any(
            getattr(db_report, key) != value for key, value in fields.items()
        )
        if dry_run or not changed:
            return changed
        for key, value in fields.items():
            setattr(db_report, key, value)
        replacement = [tag for tag in db_report.tags if tag.dimension not in owned]
        for dimension, value in dict.fromkeys(tag_pairs):
            tag = (
                self.session.query(Tag)
                .filter(Tag.dimension == dimension, Tag.value == value)
                .first()
            )
            if not tag:
                tag = Tag(dimension=dimension, value=value)
                self.session.add(tag)
                self.session.flush()
            replacement.append(tag)
        db_report.tags = replacement
        return True

    def batch_upsert(self, reports: list[ClassifiedReport]) -> StorageResult:
        result = StorageResult(total_processed=len(reports))
        for r in reports:
            try:
                action, rid = self.upsert_report(r)
                if action == "added":
                    result.newly_added += 1
                else:
                    result.updated += 1
                result.report_ids.append(rid)
            except Exception as e:
                self.session.rollback()
                result.errors.append(f"{r.search_result.title}: {e}")
        result.duplicate_skipped = (
            result.total_processed
            - result.newly_added
            - result.updated
            - len(result.errors)
        )
        return result

    def list_reports(self, params: ReportListParams) -> tuple[list[Report], int]:
        q = self.session.query(Report)

        if params.search:
            term = f"%{params.search}%"
            q = q.filter(or_(Report.title.ilike(term), Report.abstract.ilike(term)))
        if params.market:
            q = q.filter(Report.markets.contains(params.market))
        if params.asset_class:
            q = q.filter(Report.asset_classes.contains(params.asset_class))
        if params.frequency:
            q = q.filter(Report.frequencies.contains(params.frequency))
        if params.topic:
            q = q.filter(Report.topics.contains(params.topic))
        if params.source:
            q = q.filter(Report.source == params.source)
        if params.has_full_text is not None:
            q = q.filter(Report.has_full_text == params.has_full_text)
        if params.date_from:
            q = q.filter(Report.published_date >= params.date_from)
        if params.date_to:
            q = q.filter(Report.published_date <= params.date_to)

        total = q.count()

        sort_col = getattr(Report, params.sort_by, Report.created_at)
        if params.sort_by in ("created_at", "published_date"):
            q = q.order_by(desc(sort_col))
        else:
            q = q.order_by(asc(sort_col))

        reports = q.offset(params.offset).limit(params.limit).all()
        return reports, total

    def get_report(self, report_id: int) -> Optional[Report]:
        return self.session.query(Report).filter(Report.id == report_id).first()

    def delete_report(self, report_id: int) -> bool:
        report = self.get_report(report_id)
        if not report:
            return False
        self.session.delete(report)
        self.session.commit()
        return True

    def update_analysis(self, report_id: int, analysis_json: str) -> bool:
        report = self.get_report(report_id)
        if not report:
            return False
        report.analysis_json = analysis_json
        self.session.commit()
        return True

    def update_knowledge_card(self, report_id: int, knowledge_card_json: str) -> bool:
        report = self.get_report(report_id)
        if not report:
            return False
        report.knowledge_card_json = knowledge_card_json
        self.session.commit()
        return True

    def update_summary(self, report_id: int, summary: str) -> bool:
        report = self.get_report(report_id)
        if not report:
            return False
        report.summary = summary
        self.session.commit()
        return True

    def update_selection(
        self,
        report_id: int,
        score: float,
        reason: str,
        is_selected: bool,
        *,
        direction=None,
        relevance=None,
        provenance_json=None,
        commit: bool = True,
    ) -> bool:
        """Update LLM selection score and status for a report."""
        report = self.get_report(report_id)
        if not report:
            return False
        report.selection_score = score
        report.selection_reason = reason
        report.is_selected = is_selected
        report.selection_direction = direction
        report.selection_relevance = relevance
        report.selection_provenance_json = provenance_json
        if commit:
            self.session.commit()
        return True

    def get_unjudged_reports(
        self,
        since=None,
        limit: int = 200,
        published_since=None,
        scoring_fingerprint: str | None = None,
    ) -> list[Report]:
        """Get reports that haven't been LLM-scored yet (selection_score == 0 and no reason)."""
        from datetime import datetime, timezone

        q = self.session.query(Report)
        if scoring_fingerprint is None:
            q = q.filter(
                Report.selection_score == 0.0, Report.selection_reason.is_(None)
            )
        if since:
            q = q.filter(Report.created_at >= since)
        if published_since:
            q = q.filter(
                Report.published_date >= published_since,
                Report.published_date <= datetime.now(timezone.utc),
            )
        q = q.order_by(desc(Report.created_at))
        if scoring_fingerprint is None:
            return q.limit(limit).all()
        results = []
        for row in q.yield_per(200):
            try:
                provenance = json.loads(row.selection_provenance_json or "{}")
            except (TypeError, ValueError):
                provenance = {}
            from reportagent.services.paper_scoring import evidence_fingerprint

            stale_material = (
                isinstance(provenance, dict)
                and provenance.get("status") in ("scored", "pending_needs_material")
                and provenance.get("evidence_fingerprint") != evidence_fingerprint(row)
            )
            if (
                not isinstance(provenance, dict)
                or provenance.get("fingerprint") != scoring_fingerprint
                or provenance.get("status") == "retryable_model_failure"
                or stale_material
            ):
                results.append(row)
                if len(results) >= limit:
                    break
        return results

    def get_selected_reports(self, since=None, limit: int = 100) -> list[Report]:
        """Get LLM-selected reports for daily push."""
        q = self.session.query(Report).filter(Report.is_selected.is_(True))
        if since:
            q = q.filter(Report.created_at >= since)
        return q.order_by(desc(Report.selection_score)).limit(limit).all()

    def get_stats(self) -> dict:
        total = self.session.query(Report).count()
        with_text = (
            self.session.query(Report).filter(Report.has_full_text.is_(True)).count()
        )
        return {
            "total_reports": total,
            "with_full_text": with_text,
            "without_full_text": total - with_text,
        }


class SearchHistoryRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, task_id: str, criteria_json: str) -> SearchHistory:
        record = SearchHistory(task_id=task_id, criteria_json=criteria_json)
        self.session.add(record)
        self.session.commit()
        return record

    def update_status(
        self,
        task_id: str,
        status: str,
        results_count: int = 0,
        newly_added: int = 0,
        errors: Optional[list[str]] = None,
    ):
        record = (
            self.session.query(SearchHistory)
            .filter(SearchHistory.task_id == task_id)
            .first()
        )
        if record:
            record.status = status
            record.results_count = results_count
            record.newly_added = newly_added
            if errors:
                record.errors_json = json.dumps(errors)
            if status in ("completed", "failed"):
                record.completed_at = datetime.utcnow()
            self.session.commit()

    def get(self, task_id: str) -> Optional[SearchHistory]:
        return (
            self.session.query(SearchHistory)
            .filter(SearchHistory.task_id == task_id)
            .first()
        )

    def list_recent(self, limit: int = 20) -> list[SearchHistory]:
        return (
            self.session.query(SearchHistory)
            .order_by(desc(SearchHistory.created_at))
            .limit(limit)
            .all()
        )
