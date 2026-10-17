#!/usr/bin/env python3
"""Extract PDF text/tables/formulas; --report-id --save repairs library material."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from reportagent.services.pdf_extraction import extract_formulas_mineru
from reportagent.sources.base import usable_full_text

logger = logging.getLogger("parse")


def _positive_report_id(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "report ID must be a positive integer"
        ) from error
    if number <= 0:
        raise argparse.ArgumentTypeError("report ID must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description="Parse PDF report")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pdf", type=str, help="Path to PDF file")
    group.add_argument(
        "--report-id", type=_positive_report_id, help="Report ID from database"
    )
    parser.add_argument(
        "--save", action="store_true", help="Save results to DB (requires --report-id)"
    )
    parser.add_argument("--output", type=str, help="Output JSON file")
    args = parser.parse_args()
    if args.save and args.report_id is None:
        parser.error(
            "--save requires --report-id; import local PDFs through collection first"
        )
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    try:
        if args.report_id is not None:
            from reportagent.db.engine import init_db, get_session_factory
            from reportagent.services.pdf_material import parse_report_pdf

            init_db()
            with get_session_factory()() as db:
                pdf_path, tables, equations, full_text = parse_report_pdf(
                    db, args.report_id, save=args.save, parser=extract_formulas_mineru
                )
            if args.save:
                logger.info("Saved to DB")
        else:
            pdf_path = args.pdf
            if not Path(pdf_path).is_file():
                raise ValueError(f"PDF not found: {pdf_path}")
            tables, equations, full_text = extract_formulas_mineru(pdf_path)
            if not usable_full_text(full_text):
                raise ValueError("PDF has no usable extracted text")
            if equations:
                from reportagent.processors.formula_normalizer import normalize_formulas

                equations = normalize_formulas(equations)
    except Exception as error:
        logger.error("PDF parse failed: %s", error)
        raise SystemExit(1) from error
    output = {
        "pdf_path": pdf_path,
        "tables_count": len(tables),
        "equations_count": len(equations),
        "text_preview": full_text[:500],
    }
    if args.output:
        Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(f"Results saved to {args.output}")
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
