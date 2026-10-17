"""Command-line adapter for daily_push."""

import argparse
import logging


def main():
    parser = argparse.ArgumentParser(description="Daily Feishu Push — 每日论文简报推送")
    parser.add_argument(
        "--receive-ids",
        type=str,
        default="",
        help="飞书接收者 ID，逗号分隔（优先于环境变量）",
    )
    parser.add_argument(
        "--receive-type",
        type=str,
        default="",
        choices=["", "open_id", "chat_id"],
        help="接收者类型（留空则使用 .env 配置）",
    )
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际推送")
    parser.add_argument("--output", type=str, default=None, help="将简报输出到文件")
    parser.add_argument(
        "--report-ids",
        default="",
        help="指定库中论文 ID（逗号分隔），仍须满足日报公开时间窗口且有来源时间证据",
    )
    parser.add_argument(
        "--max-reviews",
        type=int,
        default=None,
        help="可选的新全文评估预算，正整数；优先于配置，未配置时评估全部符合条件的论文",
    )
    parser.add_argument(
        "--force-review", action="store_true", help="刷新已缓存的全文质量评估"
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.daily_push import DailyPushOptions, run_daily_push

    run_daily_push(DailyPushOptions(**vars(args)))


if __name__ == "__main__":
    main()
