"""Collect on a fixed schedule and save source diagnostics independently of reports."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from reportagent.utils.config import PROJECT_ROOT, get_config

logger = logging.getLogger(__name__)
DEFAULT_TOPICS = "量化策略,因子模型,高频交易,风险模型,执行算法,组合优化,统计套利,波动率,另类数据,market microstructure,factor investing,portfolio optimization"


def read_latest_collection(root: Path | None = None) -> dict | None:
    try:
        value = json.loads(((root or PROJECT_ROOT) / "data/collections/latest.json").read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("completed_at") and value.get("status") in {"ok", "partial", "failed", "cancelled"}:
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def _save(path: Path, result: dict):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run_scheduled_collection() -> dict:
    from reportagent.services.collection import CollectionOptions, collect_reports
    config = get_config("daily_collection", default={})
    directory = PROJECT_ROOT / "data/collections"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running"}
        result = {"run_id": str(uuid.uuid4()), "status": "running", "source_statuses": [],
                  "started_at": datetime.now(timezone.utc).isoformat()}
        try:
            sources = config.get("sources", ["arxiv", "eastmoney", "bigquant"])
            options = CollectionOptions(topics=config.get("topics", DEFAULT_TOPICS),
                sources=",".join(sources), days=config.get("lookback_days", 3),
                max_results=config.get("max_per_source", 15), no_analysis=True, broad=True)
            async def collect():
                return await asyncio.wait_for(collect_reports(options), timeout=900)
            response = asyncio.run(collect())
            if not isinstance(response, dict) or response.get("status") not in {"ok", "partial", "failed"}:
                raise ValueError("Collector returned no verifiable outcome")
            result.update(response)
        except Exception as error:
            result.update(status="failed", error=type(error).__name__)
            logger.error("Scheduled collection failed: %s", type(error).__name__)
        finally:
            result["completed_at"] = datetime.now(timezone.utc).isoformat()
            _save(directory / f"{result['run_id']}.json", result)
            _save(directory / "latest.json", result)
        logger.info("Scheduled collection %s: %s", result["run_id"], result["status"])
        return result
