"""Command-line adapter for direction_scoring."""

import argparse
import logging
import asyncio


def main():
    parser = argparse.ArgumentParser(
        description="LLM 方向过滤 — 对论文进行6大研究方向评分"
    )
    parser.add_argument(
        "--days", type=int, default=1, help="过滤最近 N 天的论文 (default: 1)"
    )
    parser.add_argument(
        "--report-ids",
        type=str,
        default="",
        help="指定论文 ID，逗号分隔（优先于 --days）",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="精选阈值（默认读取 push_directions.yaml）",
    )
    parser.add_argument("--dry-run", action="store_true", help="仅评分不入库")
    parser.add_argument(
        "--json", action="store_true", dest="json_output", help="以 JSON 格式输出结果"
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    from reportagent.services.direction_scoring import (
        DirectionOptions,
        run_direction_filter,
    )

    asyncio.run(run_direction_filter(DirectionOptions(**vars(args))))


if __name__ == "__main__":
    main()
