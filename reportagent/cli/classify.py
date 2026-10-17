"""Command-line adapter for batch_classification."""

import argparse
import logging
import asyncio


def _positive_integer(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description="Re-classify reports")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Re-classify all reports")
    group.add_argument(
        "--report-id", type=_positive_integer, help="Classify single report by ID"
    )
    group.add_argument(
        "--recent", type=_positive_integer, help="Re-classify last N reports"
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--llm", action="store_true", help="Use LLM semantic classification"
    )
    mode.add_argument(
        "--hybrid",
        action="store_true",
        help="Run rules and LLM; retain only evidence-supported model labels",
    )

    parser.add_argument(
        "--dry-run", action="store_true", help="Preview only, don't save"
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.batch_classification import (
        ClassificationOptions,
        classify_async,
    )

    audit = asyncio.run(classify_async(ClassificationOptions(**vars(args))))
    if audit["failed"]:
        logging.error(
            "Classification failed for papers %s: %s",
            audit["failed_ids"],
            audit["errors"],
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
