"""Run the configured ingestion job independently of report generation."""

import argparse
import json
import logging


def main():
    argparse.ArgumentParser(
        description="按 daily_collection 配置采集并保存来源诊断，不生成或发送报告"
    ).parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.scheduled_collection import run_scheduled_collection

    result = run_scheduled_collection()
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
