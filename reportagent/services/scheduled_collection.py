"""Collect on a fixed schedule and save source diagnostics independently of reports."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from reportagent.utils.config import PROJECT_ROOT, get_config
from reportagent.services.report_periods import REPORT_TIMEZONE

logger = logging.getLogger(__name__)
DEFAULT_TOPICS = "量化策略,因子模型,高频交易,风险模型,执行算法,组合优化,统计套利,波动率,另类数据,market microstructure,factor investing,portfolio optimization"


def _collection_error_label(error: str) -> str:
    lowered = error.lower()
    if lowered.startswith("announcement"):
        if "outside available recent lists" in lowered:
            return "公告列表未覆盖目标日期"
        if "abstract" in lowered:
            return "部分公告的摘要获取失败"
        return "公告列表获取或完整性检查失败"
    if "exceeds size limit" in lowered:
        return "PDF 超过当时的下载大小上限"
    if "timeout" in lowered:
        if lowered.startswith("pdf"):
            return "PDF 下载或解析超时"
        return "搜索请求超时" if lowered.startswith("search") else "请求超时"
    if lowered.startswith("pdf"):
        return "PDF 获取或解析失败"
    if lowered.startswith("search"):
        return "来源搜索失败"
    return "其他采集或入库异常"


def collection_notice(collection: dict | None) -> str | None:
    """Describe the saved run, without presenting historical PDF gaps as an outage."""
    if not collection or collection.get("status") not in {
        "partial",
        "failed",
        "cancelled",
    }:
        return None
    label = {"partial": "部分完成", "failed": "失败", "cancelled": "已取消"}[
        collection["status"]
    ]
    try:
        completed = datetime.fromisoformat(collection["completed_at"])
        stamp = completed.astimezone(REPORT_TIMEZONE).strftime("%Y-%m-%d %H:%M")
        heading = f"最近定时采集（{stamp}，北京时间）{label}"
    except (KeyError, ValueError, TypeError):
        heading = f"最近定时采集{label}"
    details = []
    storage = collection.get("storage") or {}
    for key, action in (("added", "新增"), ("updated", "更新")):
        count = storage.get(key)
        if type(count) is int and count >= 0:
            details.append(f"{action} {count} 篇")
    sources = collection.get("source_statuses") or []
    if sources and all(
        type(s.get("valid")) is int
        and type(s.get("full_text")) is int
        and 0 <= s["full_text"] <= s["valid"]
        for s in sources
    ):
        valid = sum(s["valid"] for s in sources)
        full = sum(s["full_text"] for s in sources)
        details.append(
            f"采集时 {valid} 条有效资料中，{full} 条取得全文、{valid - full} 条未取得全文（按来源合计）"
        )
    coverage_incomplete = collection["status"] in {"failed", "cancelled"}
    for source in sources:
        errors = source.get("errors") or []
        abnormal = source.get("status") in {
            "partial",
            "error",
            "unavailable",
            "cancelled",
        }
        if not errors and not abnormal:
            continue
        labels = list(dict.fromkeys(_collection_error_label(e) for e in errors))
        name = {"arxiv": "arXiv", "eastmoney": "东方财富", "bigquant": "BigQuant"}.get(
            source["source"], source["source"]
        )
        details.append(f"{name}：{'、'.join(labels) or '来源采集未完成'}")
        coverage_incomplete |= abnormal and (
            not errors or any(not e.lower().startswith("pdf") for e in errors)
        )
    if collection.get("error"):
        details.append(_collection_error_label(collection["error"]))
    if not details:
        details.append("记录未提供具体失败阶段")
    if coverage_incomplete:
        details.append("来源覆盖可能不完整")
    return heading + "：" + "；".join(details) + "。"


def read_latest_collection(root: Path | None = None) -> dict | None:
    try:
        value = json.loads(
            ((root or PROJECT_ROOT) / "data/collections/latest.json").read_text(
                encoding="utf-8"
            )
        )
        if (
            isinstance(value, dict)
            and value.get("completed_at")
            and value.get("status") in {"ok", "partial", "failed", "cancelled"}
        ):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def _save(path: Path, result: dict):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def run_scheduled_collection() -> dict:
    from reportagent.services.collection import collect_reports
    from reportagent.models.schemas import UserCriteria, SourceType
    from reportagent.services.report_periods import get_daily_range

    config = get_config("daily_collection", default={})
    directory = PROJECT_ROOT / "data/collections"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running"}
        result = {
            "run_id": str(uuid.uuid4()),
            "status": "running",
            "source_statuses": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        started = monotonic()
        try:
            sources = config.get("sources", ["arxiv", "eastmoney", "bigquant"])
            start, end, report_date, _ = get_daily_range()
            result["publication_window"] = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "end_exclusive": True,
            }
            result["report_date"] = report_date
            criteria = UserCriteria(
                topics=[
                    topic.strip()
                    for topic in config.get("topics", DEFAULT_TOPICS).split(",")
                    if topic.strip()
                ],
                sources=[SourceType(name) for name in sources],
                date_from=start,
                date_to=end,
                date_basis="announcement",
                date_to_exclusive=True,
                max_results_per_source=None,
            )
            response = asyncio.run(
                collect_reports(criteria, no_analysis=True, broad=True)
            )
            if not isinstance(response, dict) or response.get("status") not in {
                "ok",
                "partial",
                "failed",
            }:
                raise ValueError("Collector returned no verifiable outcome")
            result.update(response)
        except Exception as error:
            result.update(status="failed", error=type(error).__name__)
            logger.error("Scheduled collection failed: %s", type(error).__name__)
        finally:
            result["completed_at"] = datetime.now(timezone.utc).isoformat()
            result["elapsed_ms"] = round((monotonic() - started) * 1000)
            _save(directory / f"{result['run_id']}.json", result)
            _save(directory / "latest.json", result)
        logger.info("Scheduled collection %s: %s", result["run_id"], result["status"])
        return result
