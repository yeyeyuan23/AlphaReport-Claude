"""Run the configured ingestion job independently of report generation."""

import argparse
from datetime import date
import json
import logging


def _source_summary(source):
    screening = source.get("screening", {})
    return (
        f"{source['source']} [{source['status']}]：读取 {source.get('fetched', 0)} 条元数据；"
        f"相关性检查 {screening.get('checked', 0)} 篇，"
        f"通过 {screening.get('selected', 0)} 篇，"
        f"低相关 {screening.get('rejected', 0)} 篇，"
        f"缺摘要 {screening.get('pending', 0)} 篇，"
        f"评分失败 {screening.get('failed', 0)} 篇，"
        f"复用评分 {screening.get('cached_scores', 0)} 篇；"
        f"分类并入库 {source.get('valid', 0)} 篇，"
        f"全文可用 {source.get('full_text', 0)}/{source.get('valid', 0)} 篇"
    )


def _review_summary(review):
    if review.get("error"):
        return f"全文阅读评估失败：{review['error']}"
    calls = set(review.get("review_ids", []))
    completed = review.get("completed_ids")
    count = len(completed) if completed is not None else "未记录"
    unfinished = len(calls - set(completed or []))
    return (
        f"全文阅读评估（含库中待处理）：新调用 {len(calls)} 篇，完成 {count} 篇，"
        f"缓存有效 {len(review.get('cached_ids', []))} 篇，"
        f"缺材料 {len(review.get('pending_material_ids', []))} 篇，"
        f"缺全文或超长跳过 {len(review.get('skipped_material_ids', []))} 篇，"
        f"预算延期 {len(review.get('deferred_ids', []))} 篇，"
        f"新调用未完成 {unfinished} 篇"
    )


def collection_summary(result):
    if result["status"] == "already_running":
        return ["已有采集任务运行，本次未执行。"]
    lines = [f"采集流程状态：{result['status']}"]
    window = result.get("publication_window")
    if window:
        lines.append(f"来源日期：{window['start']} 至 {window['end']}")
    lines.extend(
        _source_summary(source) for source in result.get("source_statuses", [])
    )
    storage = result.get("storage")
    if storage:
        lines.append(
            f"入库：新增 {storage.get('added', 0)} 篇，更新 {storage.get('updated', 0)} 篇"
        )
    if result.get("review"):
        lines.append(_review_summary(result["review"]))
    stages = result.get("timing", {}).get("stages_ms", {})
    labels = {
        "metadata": "来源元数据",
        "relevance": "相关性筛选",
        "classification": "分类",
        "storage": "入库",
        "download_parse": "全文获取与解析",
        "full_text_review": "全文阅读评估",
    }
    if stages:
        lines.append(
            "阶段耗时："
            + "，".join(
                f"{labels.get(k, k)} {v / 1000:.1f} 秒" for k, v in stages.items()
            )
        )
    if "elapsed_ms" in result:
        lines.append(f"总耗时：{result['elapsed_ms'] / 1000:.1f} 秒")
    if result.get("error"):
        lines.append(f"流程错误：{result['error']}")
    return lines


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
    for line in collection_summary(result):
        logging.info(line)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
