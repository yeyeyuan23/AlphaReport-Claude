"""Command-line adapter for narrative_brief."""
import argparse
import logging
import asyncio


def main():
    parser = argparse.ArgumentParser(description="Generate narrative briefs (Knowledge Cards)")
    parser.add_argument("--report-id", type=int, help="Process single report by ID")
    parser.add_argument("--recent", type=int, help="Process N most recent reports")
    parser.add_argument("--force", action="store_true", help="Regenerate even if already exists")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.narrative_brief import NarrativeOptions, run_narrative_brief

    asyncio.run(run_narrative_brief(NarrativeOptions(**vars(args))))


if __name__ == "__main__":
    main()
