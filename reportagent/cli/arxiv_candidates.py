"""Count configured arXiv announcement/topic candidates without models or PDFs."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from time import monotonic


async def inspect_candidates(report_date=None):
    from reportagent.models.schemas import UserCriteria, SourceType
    from reportagent.services.report_periods import get_daily_range
    from reportagent.sources.factory import build_sources

    start, end, label, _ = get_daily_range(report_date)
    source = build_sources(["arxiv"])[0]
    source.download_pdfs = False
    started_at, started = datetime.now(timezone.utc), monotonic()
    rows = await source.search_broad(
        UserCriteria(
            topics=[],
            sources=[SourceType.ARXIV],
            date_basis="announcement",
            date_from=start,
            date_to=end,
            date_to_exclusive=True,
            max_results_per_source=None,
        )
    )
    return {
        "status": "partial" if source.last_search.errors else "ok",
        "report_date": label,
        "publication_window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "end_exclusive": True,
        },
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": round((monotonic() - started) * 1000),
        "discovery": source.discovery_stats,
        "errors": source.last_search.errors,
        "candidate_count": len(rows),
        "note": "Category/topic matches only; model relevance and full-text evidence have not been evaluated.",
        "candidates": [
            {
                "arxiv_id": r.arxiv_id,
                "title": r.title,
                "abstract": r.abstract,
                "announced_at": r.announced_at.isoformat(),
                "scope": r.raw_metadata.get("arxiv_search_scope"),
                "listing": r.raw_metadata.get("announcement_info"),
            }
            for r in rows
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="统计完整公告窗口内的 arXiv 分类/主题候选；不调用模型、不下载 PDF、不写库、不发送消息"
    )
    parser.add_argument(
        "--report-date",
        help="北京时间报告日期 YYYY-MM-DD，默认今天；查询此前 08:00 至当日 08:00",
    )
    parser.add_argument("--output", help="保存完整候选和计时 JSON")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    result = asyncio.run(inspect_candidates(args.report_date))
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "candidates"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
