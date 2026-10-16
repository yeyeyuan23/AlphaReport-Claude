"""Command-line adapter for collection."""
import argparse
import logging
import asyncio
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Collect research reports")
    parser.add_argument("--topics", type=str, required=True,
                        help="Comma-separated topics (e.g. '因子模型,高频交易')")
    parser.add_argument("--sources", type=str, default="arxiv,eastmoney,bigquant",
                        help="Comma-separated sources: arxiv,eastmoney,bigquant,local_pdf")
    parser.add_argument("--keywords", type=str, default="",
                        help="Additional keywords")
    parser.add_argument("--max", type=int, default=20, dest="max_results",
                        help="Max results per source (default: 20)")
    parser.add_argument("--days", type=int, default=60,
                        help="Lookback days for date filtering (default: 60, i.e. last 2 months)")
    parser.add_argument("--date-from", type=str, default=None,
                        help="Date filter from (YYYY-MM-DD), overrides --days")
    parser.add_argument("--date-to", type=str, default=None,
                        help="Date filter to (YYYY-MM-DD), defaults to today")
    parser.add_argument("--no-analysis", action="store_true",
                        help="Skip deep analysis after collection")
    parser.add_argument("--analysis-depth", type=str, default="standard",
                        choices=["quick", "standard", "deep"],
                        help="Analysis depth (default: standard)")
    parser.add_argument("--broad", action="store_true",
                        help="Broad search: pull ALL q-fin papers without keyword filter (全量拉取)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output JSON file (default: print to stdout)")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.collection import CollectionOptions, collect_reports

    workflow_options = vars(args).copy()
    workflow_options.pop("output")
    result = asyncio.run(collect_reports(CollectionOptions(**workflow_options)))

    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"Results saved to {args.output}")
    else:
        # Single-line JSON: daily_feishu_push.py parses stdout line-by-line looking
        # for a line starting with '{"topics"'. Pretty-printed (indent=2) output
        # broke that match and produced a false "Collection done: {}" alarm.
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
