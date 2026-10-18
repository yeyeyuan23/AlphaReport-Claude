"""Command-line adapter for weekly_report."""

import argparse
import logging


def main():
    parser = argparse.ArgumentParser(description="Weekly Report — 每周论文周报推送")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际推送")
    parser.add_argument(
        "--with-summary",
        action="store_true",
        help="预览时也调用配置的模型生成周报总结（消耗模型额度）",
    )
    parser.add_argument(
        "--weeks-ago",
        type=int,
        default=0,
        help="0=含今天在内最近七个日期，1=窗口前移7天（用于补发）",
    )
    parser.add_argument(
        "--receive-ids", type=str, default="", help="飞书接收者 ID，逗号分隔"
    )
    parser.add_argument(
        "--receive-type",
        type=str,
        default="",
        choices=["", "open_id", "chat_id"],
        help="接收者类型",
    )
    parser.add_argument("--output", type=str, default=None, help="将周报输出到文件")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.weekly_report import (
        WeeklyReportOptions,
        run_weekly_report,
    )

    result = run_weekly_report(WeeklyReportOptions(**vars(args)))
    if result["status"] in {"failed", "partial"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
