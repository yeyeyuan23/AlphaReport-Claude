"""Command-line adapter for batch_analysis."""
import argparse
import logging
import asyncio
import json


def main():
    parser = argparse.ArgumentParser(description="Deep analysis for research reports")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Analyze all reports")
    group.add_argument("--unanalyzed", action="store_true", help="Analyze only reports without analysis")
    group.add_argument("--report-id", type=int, help="Analyze single report by ID")
    group.add_argument("--recent", type=int, help="Analyze last N reports")
    group.add_argument("--topic", help="Analyze reports with this exact classification topic token")

    parser.add_argument("--depth", type=str, default="standard",
                        choices=["quick", "standard", "deep"],
                        help="Analysis depth (default: standard)")
    parser.add_argument("--force", action="store_true",
                        help="Re-analyze even if analysis exists")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.batch_analysis import AnalysisOptions, run_analysis

    result = asyncio.run(run_analysis(AnalysisOptions(**vars(args))))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
