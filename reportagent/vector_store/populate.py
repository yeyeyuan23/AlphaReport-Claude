#! /usr/bin/env python3
"""Rebuild vector store index from existing reports in the SQL database.

Usage:
    python -m reportagent.vector_store.populate
    python -m reportagent.vector_store.populate --report-id 42
"""
from __future__ import annotations

import argparse
import logging

from reportagent.classifiers.quant_filter import compute_quant_score
from reportagent.db.engine import get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.models.schemas import ReportListParams
from reportagent.vector_store.store import VectorStore, get_vector_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("vector_store_populate")


def main():
    parser = argparse.ArgumentParser(description="Rebuild ChromaDB index from report library")
    parser.add_argument("--report-id", type=int, help="Index only a single report by ID")
    parser.add_argument("--source", type=str, help="Filter by source (arxiv, eastmoney, bigquant, local_pdf)")
    parser.add_argument("--limit", type=int, default=0, help="Max reports to index (0 = all)")
    parser.add_argument("--has-full-text", action="store_true", default=True,
                        help="Only index reports with full text (default: true)")
    parser.add_argument("--clear", action="store_true", help="Clear collection before indexing")
    args = parser.parse_args()

    store = get_vector_store()

    if args.clear:
        logger.info("Clearing existing collection...")
        store._client.delete_collection("report_chunks")
        store = VectorStore()

    factory = get_session_factory()
    session = factory()
    repo = ReportRepository(session)

    try:
        if args.report_id:
            report = repo.get_report(args.report_id)
            if not report:
                logger.error("Report %d not found", args.report_id)
                return
            reports = [report]
        else:
            params = ReportListParams(
                limit=args.limit if args.limit > 0 else 100000,
                source=args.source,
                has_full_text=args.has_full_text,
            )
            reports, total = repo.list_reports(params)
            logger.info("Found %d reports to index", total)

        indexed_count = 0
        chunk_count = 0

        for db_report in reports:
            text = db_report.full_text or db_report.abstract or ""
            if not text.strip():
                continue

            quant_score, _ = compute_quant_score(db_report.title, db_report.abstract or "")
            chunks = store.index_report(
                report_id=db_report.id,
                title=db_report.title,
                text=text,
                source=db_report.source or "",
                topics=db_report.topics or "",
                markets=db_report.markets or "",
                quant_score=quant_score,
            )
            if chunks:
                indexed_count += 1
                chunk_count += chunks
                if indexed_count % 10 == 0:
                    logger.info("Indexed %d reports (%d chunks)...", indexed_count, chunk_count)

        logger.info("Done: indexed %d reports, %d total chunks", indexed_count, chunk_count)
    finally:
        session.close()


if __name__ == "__main__":
    main()
