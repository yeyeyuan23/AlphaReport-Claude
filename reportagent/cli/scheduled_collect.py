"""Run the configured ingestion job independently of report generation."""

import argparse
from datetime import date
import json
import logging


def main():
    parser = argparse.ArgumentParser(
        description="按 daily_collection 配置采集并保存来源诊断，不生成或发送报告"
    )
    parser.add_argument(
        "--report-date",
        type=date.fromisoformat,
        help="假定报告日 YYYY-MM-DD，采集其前一天；省略则使用北京时间今天",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.scheduled_collection import run_scheduled_collection

    result = run_scheduled_collection(
        report_date=args.report_date.isoformat() if args.report_date else None
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
