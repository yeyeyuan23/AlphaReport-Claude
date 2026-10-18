"""Command-line adapter for paper_scoring."""

import argparse
import logging
import asyncio


def main():
    parser = argparse.ArgumentParser(
        description="LLM-based paper filtering and scoring for daily push curation"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Lookback days for unjudged papers (default: 1 = today only)",
    )
    parser.add_argument(
        "--all-unjudged",
        action="store_true",
        help="Score ALL unjudged papers regardless of date",
    )
    parser.add_argument(
        "--published-days",
        type=int,
        help="Retry unjudged papers published in this many days, regardless of ingestion date",
    )
    parser.add_argument(
        "--published-date",
        help="Score source publications dated the day before this report date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--created-date",
        help="Score papers first ingested on this Beijing date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Relevance threshold (default: push_directions.yaml, fallback 6.0)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=10, help="Papers per LLM batch (default: 10)"
    )
    parser.add_argument(
        "--max-papers",
        type=int,
        default=200,
        help="Maximum papers to score (default: 200; 0 = unlimited)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview scores without writing to DB"
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.paper_scoring import ScoringOptions, score_reports

    if args.days < 1 or (args.published_days is not None and args.published_days < 1):
        parser.error("Lookback days must be positive")
    asyncio.run(score_reports(ScoringOptions(**vars(args))))


if __name__ == "__main__":
    main()
