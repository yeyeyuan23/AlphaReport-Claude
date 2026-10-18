#!/usr/bin/env python3
"""
CLI Database Operations — query and update reports
Usage:
    python -m reportagent.cli.db_query --list --limit 20                     # List reports
    python -m reportagent.cli.db_query --get 123                              # Get report details
    python -m reportagent.cli.db_query --search "因子模型" --limit 10         # Search reports
    python -m reportagent.cli.db_query --update-analysis 123 --file analysis.json  # Save analysis
    python -m reportagent.cli.db_query --update-summary 123 --text "summary..."    # Save summary
    python -m reportagent.cli.db_query --stats                                # DB stats
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


from reportagent.db.engine import init_db, get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.models.schemas import ReportListParams
from reportagent.services.report_reads import get_report_detail

logger = logging.getLogger("db")


def _report_to_dict(r) -> dict:
    custom_tags = [t.value for t in r.tags if t.dimension == "custom"] if r.tags else []
    return {
        "id": r.id,
        "title": r.title,
        "authors": json.loads(r.authors) if r.authors else [],
        "abstract": r.abstract,
        "source": r.source,
        "material_type": r.material_type or "unknown",
        "material_type_info": json.loads(r.material_type_info_json)
        if r.material_type_info_json
        else {},
        "source_url": r.source_url,
        "doi": r.doi,
        "arxiv_id": r.arxiv_id,
        "published_date": r.published_date.isoformat() if r.published_date else None,
        "publication_date_info": json.loads(r.publication_date_info_json)
        if r.publication_date_info_json
        else {},
        "has_full_text": r.has_full_text,
        "markets": r.markets.split(",") if r.markets else [],
        "asset_classes": r.asset_classes.split(",") if r.asset_classes else [],
        "frequencies": r.frequencies.split(",") if r.frequencies else [],
        "topics": r.topics.split(",") if r.topics else [],
        "custom_tags": custom_tags,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "classification_confidence": r.classification_confidence,
        "classification_method": r.classification_method,
        "classification_provenance": json.loads(r.classification_provenance_json)
        if r.classification_provenance_json
        else {},
    }


def main():
    parser = argparse.ArgumentParser(description="Database operations")
    parser.add_argument("--list", action="store_true", help="List reports")
    parser.add_argument("--get", type=int, help="Get report by ID")
    parser.add_argument("--search", type=str, help="Search reports")
    parser.add_argument("--stats", action="store_true", help="Show DB stats")
    parser.add_argument(
        "--update-analysis", type=int, help="Update analysis for report ID"
    )
    parser.add_argument(
        "--update-summary", type=int, help="Update summary for report ID"
    )
    parser.add_argument(
        "--update-knowledge-card", type=int, help="Update knowledge card for report ID"
    )
    parser.add_argument(
        "--file",
        type=str,
        help="JSON file for --update-analysis / --update-knowledge-card",
    )
    parser.add_argument("--text", type=str, help="Text for --update-summary")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--full", action="store_true", help="Include full text in output"
    )
    parser.add_argument("--json-output", type=str, help="Output JSON file")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")

    init_db()
    factory = get_session_factory()
    session = factory()

    try:
        repo = ReportRepository(session)

        # ── Stats ──
        if args.stats:
            stats = repo.get_stats()
            print(json.dumps(stats, ensure_ascii=False, indent=2))

        # ── List ──
        elif args.list:
            params = ReportListParams(limit=args.limit, sort_by="created_at")
            reports, total = repo.list_reports(params)
            result = {"total": total, "reports": [_report_to_dict(r) for r in reports]}
            if args.json_output:
                Path(args.json_output).write_text(
                    json.dumps(result, ensure_ascii=False, indent=2)
                )
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))

        # ── Get ──
        elif args.get:
            data = get_report_detail(
                session,
                args.get,
                serialize=_report_to_dict,
                include_knowledge_card=True,
            )
            if data is None:
                print(json.dumps({"error": f"Report {args.get} not found"}))
                sys.exit(1)
            for key in ("tables_json", "equations_json"):
                data.pop(key, None)
            if not args.full:
                data.pop("full_text", None)
                data.pop("summary", None)
            if args.json_output:
                Path(args.json_output).write_text(
                    json.dumps(data, ensure_ascii=False, indent=2)
                )
            else:
                print(json.dumps(data, ensure_ascii=False, indent=2))

        # ── Search ──
        elif args.search:
            params = ReportListParams(search=args.search, limit=args.limit)
            reports, total = repo.list_reports(params)
            result = {
                "query": args.search,
                "total": total,
                "reports": [_report_to_dict(r) for r in reports],
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))

        # ── Update Analysis ──
        elif args.update_analysis:
            if args.file:
                analysis = Path(args.file).read_text(encoding="utf-8")
            else:
                analysis = sys.stdin.read()
            repo.update_analysis(args.update_analysis, analysis)
            logger.info(f"Analysis saved for report {args.update_analysis}")

        # ── Update Summary ──
        elif args.update_summary:
            text = args.text or sys.stdin.read().strip()
            repo.update_summary(args.update_summary, text)
            logger.info(f"Summary saved for report {args.update_summary}")

        # ── Update Knowledge Card ──
        elif args.update_knowledge_card:
            if args.file:
                kc = Path(args.file).read_text(encoding="utf-8")
            else:
                kc = sys.stdin.read()
            r = repo.get_report(args.update_knowledge_card)
            if r:
                r.knowledge_card_json = kc
                session.commit()
                logger.info(
                    f"Knowledge card saved for report {args.update_knowledge_card}"
                )
            else:
                logger.error(f"Report {args.update_knowledge_card} not found")

    finally:
        session.close()


if __name__ == "__main__":
    main()
