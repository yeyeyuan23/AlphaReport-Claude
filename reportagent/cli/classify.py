"""Command-line adapter for batch_classification."""
import argparse
import logging
import asyncio


def main():
    parser = argparse.ArgumentParser(description="Re-classify reports")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Re-classify all reports")
    group.add_argument("--report-id", type=int, help="Classify single report by ID")
    group.add_argument("--recent", type=int, help="Re-classify last N reports")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--llm", action="store_true", help="Use LLM semantic classification")
    mode.add_argument("--hybrid", action="store_true", help="Use rule + LLM merged (best of both)")

    parser.add_argument("--dry-run", action="store_true", help="Preview only, don't save")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.batch_classification import ClassificationOptions, classify_async

    asyncio.run(classify_async(ClassificationOptions(**vars(args))))


if __name__ == "__main__":
    main()
