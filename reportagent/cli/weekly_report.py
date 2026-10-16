"""Command-line adapter for weekly_report."""
import argparse
import logging


def main():
    parser = argparse.ArgumentParser(description="Weekly Report — 每周论文周报推送")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际推送")
    parser.add_argument("--weeks-ago", type=int, default=0,
                        help="0=上周, 1=上上周（用于补发）")
    parser.add_argument("--receive-ids", type=str, default="",
                        help="飞书接收者 ID，逗号分隔")
    parser.add_argument("--receive-type", type=str, default="",
                        choices=["", "open_id", "chat_id"], help="接收者类型")
    parser.add_argument("--output", type=str, default=None, help="将周报输出到文件")
    parser.add_argument("--no-llm", action="store_true", help="不刷新模型评估；过期或缺失的证据显示待核验")

    parser.add_argument("--max-reviews", type=int, default=12, help="全文评估数量，1–12")
    parser.add_argument("--force-review", action="store_true", help="强制刷新全文评估")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.weekly_report import WeeklyReportOptions, run_weekly_report

    result = run_weekly_report(WeeklyReportOptions(**vars(args)))
    if result["status"] in {"failed", "partial"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
