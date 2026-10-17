#!/usr/bin/env python3
"""
CLI PDF Parser — extract text, formulas, and tables from PDFs
Usage:
    python -m reportagent.cli.parse_pdf --pdf path/to/paper.pdf
    python -m reportagent.cli.parse_pdf --report-id 123           # Find and parse report PDF
    python -m reportagent.cli.parse_pdf --report-id 123 --save    # Parse and save to DB
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from reportagent.services.pdf_extraction import extract_formulas_mineru

logger = logging.getLogger("parse")


def main():
    parser = argparse.ArgumentParser(description="Parse PDF report")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pdf", type=str, help="Path to PDF file")
    group.add_argument("--report-id", type=int, help="Report ID from database")
    parser.add_argument("--save", action="store_true", help="Save results to DB")
    parser.add_argument("--output", type=str, help="Output JSON file")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")

    pdf_path = None
    report = None

    if args.report_id:
        from reportagent.db.engine import init_db, get_session_factory
        from reportagent.db.repository import ReportRepository

        init_db()
        factory = get_session_factory()
        session = factory()
        try:
            repo = ReportRepository(session)
            report = repo.get_report(args.report_id)
            if not report:
                logger.error(f"Report {args.report_id} not found")
                sys.exit(1)
            pdf_path = report.pdf_path
            if not pdf_path or not Path(pdf_path).exists():
                # Try to fetch arXiv PDF
                if report.arxiv_id and report.source == "arxiv":
                    import httpx
                    import asyncio

                    async def fetch():
                        url = f"https://arxiv.org/pdf/{report.arxiv_id}"
                        async with httpx.AsyncClient(
                            timeout=60, follow_redirects=True
                        ) as c:
                            resp = await c.get(url)
                            resp.raise_for_status()
                            pdf_dir = Path("data/pdfs")
                            pdf_dir.mkdir(parents=True, exist_ok=True)
                            local = pdf_dir / f"{report.arxiv_id}.pdf"
                            local.write_bytes(resp.content)
                            return str(local)

                    pdf_path = asyncio.run(fetch())
                    logger.info(f"Downloaded PDF: {pdf_path}")
                else:
                    logger.error("No PDF available for this report")
                    sys.exit(1)
        finally:
            session.close()

    if args.pdf:
        pdf_path = args.pdf

    if not pdf_path or not Path(pdf_path).exists():
        logger.error(f"PDF not found: {pdf_path}")
        sys.exit(1)

    logger.info(f"Parsing: {pdf_path}")

    # Extract
    tables, equations, full_text = extract_formulas_mineru(pdf_path)

    # Normalize formulas
    if equations:
        try:
            from reportagent.processors.formula_normalizer import normalize_formulas

            equations = normalize_formulas(equations)
        except Exception:
            pass

    # Save to DB
    if args.save and report:
        from reportagent.db.engine import get_session_factory

        factory = get_session_factory()
        session = factory()
        try:
            if tables:
                report.tables_json = json.dumps(tables, ensure_ascii=False)
            if equations:
                report.equations_json = json.dumps(equations, ensure_ascii=False)
            if full_text:
                report.full_text = full_text
                report.has_full_text = True
            session.commit()
            logger.info("Saved to DB")
        finally:
            session.close()

    # Output
    output = {
        "pdf_path": pdf_path,
        "tables_count": len(tables),
        "equations_count": len(equations),
        "text_preview": full_text[:500] if full_text else "",
    }

    if args.output:
        Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(f"Results saved to {args.output}")
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
