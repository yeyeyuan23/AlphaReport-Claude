"""Read the paper library, assess daily candidates, and send the resulting report."""

from __future__ import annotations

from dataclasses import dataclass
import json
import asyncio
import uuid
import fcntl
from contextlib import contextmanager
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


from dotenv import load_dotenv


from reportagent.db.engine import init_db, get_session_factory
from reportagent.utils.config import get_config, PROJECT_ROOT
from reportagent.services.public_url import sync_public_url, get_web_base_url
from reportagent.services.report_quality import (
    review_report_ids,
    select_candidates,
    pending_review,
    review_fingerprint,
    resolve_review_budget,
)
from reportagent.services.report_briefing import render_briefing
from reportagent.services.feishu_markdown import card_sections
from reportagent.services.paper_scoring import (
    scoring_fingerprint as scoring_fingerprint,
    load_enabled_directions,
    screening_is_current,
)
from reportagent.services.scheduled_collection import read_latest_collection
from reportagent.services.report_periods import get_daily_range

load_dotenv()

logger = logging.getLogger("daily_push")

# ── Config ──
cfg = get_config("feishu", default={})
APP_ID = os.environ.get(cfg.get("app_id_env", "FEISHU_APP_ID"), "")
APP_SECRET = os.environ.get(cfg.get("app_secret_env", "FEISHU_APP_SECRET"), "")

# UI base URL for generating paper links
UI_BASE_URL = get_web_base_url()

# Feishu receive targets — comma-separated open_ids or chat_ids
FEISHU_RECEIVE_IDS = (
    os.environ.get("FEISHU_DAILY_PUSH_IDS", "").split(",")
    if os.environ.get("FEISHU_DAILY_PUSH_IDS")
    else []
)
FEISHU_RECEIVE_TYPE = os.environ.get(
    "FEISHU_DAILY_PUSH_TYPE", "open_id"
)  # "open_id" | "chat_id"


# ── Load push directions from config ────────────────────
@dataclass(frozen=True)
class DailyPushOptions:
    receive_ids: str = ""
    receive_type: str = ""
    dry_run: bool = False
    no_collect: bool = True  # Compatibility flag; daily reports never collect.
    output: str | None = None
    report_ids: str = ""
    max_reviews: int | None = None
    force_review: bool = False


def _load_push_settings() -> dict:
    import yaml

    path = PROJECT_ROOT / "configs/push_directions.yaml"
    with path.open(encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get("push_settings", {})


PUSH_SETTINGS: dict = _load_push_settings()


# ═══════════════════════════════════════════════════
# Step 2: Query recent papers (rolling window)
# ═══════════════════════════════════════════════════


def _paper_metadata(row) -> dict:
    """Snapshot only the report metadata used by daily rendering and delivery."""
    return {
        "id": row.id,
        **{
            field: getattr(row, field) or ""
            for field in (
                "title",
                "authors",
                "abstract",
                "source",
                "source_url",
                "arxiv_id",
                "doi",
                "topics",
                "selection_reason",
            )
        },
        "published_date": row.published_date.isoformat() if row.published_date else "",
        "has_full_text": row.has_full_text,
        "selection_provenance_json": row.selection_provenance_json,
        "selection_score": row.selection_score or 0.0,
        "selection_relevance": row.selection_relevance,
        "selection_direction": row.selection_direction,
        "ui_url": f"{UI_BASE_URL}/library/{row.id}",
    }


def _refresh_candidate_metadata(candidates: list[dict]) -> list[dict]:
    """Refresh metadata and retain only assessments still saved against current evidence."""
    if not candidates:
        return []
    from reportagent.models.database import Report

    with get_session_factory()() as session:
        current = {
            row.id: row
            for row in session.query(Report)
            .filter(
                Report.id.in_([paper["id"] for paper in candidates]),
            )
            .all()
        }
        refreshed = []
        for paper in candidates:
            row = current.get(paper["id"])
            if row is None:
                refreshed.append(
                    {
                        **paper,
                        "evaluation": pending_review(
                            paper, "论文记录已删除，请重新获取并评估。"
                        ),
                    }
                )
                continue
            snapshot = {
                field: getattr(row, field)
                for field in (
                    "source",
                    "arxiv_id",
                    "title",
                    "abstract",
                    "full_text",
                    "has_full_text",
                    "selection_relevance",
                    "selection_score",
                    "selection_reason",
                    "selection_provenance_json",
                )
            }
            try:
                saved = json.loads(row.evaluation_json or "null")
            except (TypeError, ValueError):
                saved = None
            matches = isinstance(saved, dict) and saved == paper.get("evaluation")
            if matches and (
                saved.get("status") == "reviewed" or saved.get("fingerprint")
            ):
                model = saved.get("model")
                matches = (
                    isinstance(model, str)
                    and bool(model)
                    and saved.get("fingerprint") == review_fingerprint(snapshot, model)
                )
            evaluation = (
                saved
                if matches
                else pending_review(
                    snapshot, "准备日报期间原文或已保存评估已更新，请重新评估。"
                )
            )
            refreshed.append(
                {
                    **paper,
                    **_paper_metadata(row),
                    "ui_url": paper["ui_url"],
                    "evaluation": evaluation,
                }
            )
        return refreshed


def get_todays_papers(
    report_ids: list[int] | None = None,
    *,
    report_date: str | None = None,
) -> tuple[list[dict], str, str]:
    """Get today's newly ingested papers with current, eligible relevance scores.

    按北京时间当天的 created_at（首次入库时间），不限制原始发表日期。
    发送历史不参与筛选；显式指定论文时跳过日期和相关性过滤。

    Returns:
        (papers, date_label_start, date_label_end) — date labels for briefing title
    """
    init_db()
    factory = get_session_factory()
    session = factory()
    try:
        from reportagent.models.database import Report
        from sqlalchemy import desc

        start, end, label, _ = get_daily_range(report_date)
        logger.info(
            "Query daily additions: created_at %s ~ %s (%s Beijing), current relevance required",
            start,
            end,
            label,
        )

        q = session.query(Report)
        if report_ids:
            q = q.filter(Report.id.in_(report_ids))
        else:
            q = q.filter(
                Report.created_at >= start,
                Report.created_at <= end,
                Report.selection_relevance
                >= PUSH_SETTINGS.get("llm_filter_threshold", 6.0),
            )
        q = q.order_by(desc(Report.selection_relevance), desc(Report.published_date))
        reports = q.all()
        if not report_ids:
            reports = [r for r in reports if screening_is_current(r)]

        if not reports:
            logger.warning(
                "No eligible new additions on %s Beijing. Sending empty brief.", label
            )
            return [], label, label

        # 实际取到了哪几天的论文
        day_counts: dict[str, int] = {}
        for r in reports:
            day = (
                r.published_date.strftime("%Y-%m-%d") if r.published_date else "unknown"
            )
            day_counts[day] = day_counts.get(day, 0) + 1
        logger.info(
            "Papers by published_date: "
            + ", ".join(
                f"{d}: {day_counts[d]}" for d in sorted(day_counts, reverse=True)
            )
        )

        papers = [_paper_metadata(row) for row in reports]

        selection_count = sum(1 for p in papers if p.get("selection_score", 0) > 0)
        logger.info(
            f"Found {len(papers)} new additions for daily report {label} ({selection_count} LLM-selected)"
        )
        return papers, label, label
    finally:
        session.close()


def deep_analyze_selected(
    paper_ids: list[int], depth: str = "standard", per_paper_timeout: int = 240
) -> None:
    """Run full-depth analysis (analyze.py --report-id) for only the papers that made
    it through selection and got pushed. This runs AFTER the push so it never delays
    delivery, and only touches the handful of selected papers instead of every
    collected candidate — most candidates get rejected at selection anyway, so deep
    analysis on the whole batch is wasted work (this is what caused the daily collect
    step to blow past its 15-min timeout on 2026-07-29/30).
    """
    for rid in paper_ids:
        cmd = [
            sys.executable,
            "-m",
            "reportagent.cli.analyze",
            "--report-id",
            str(rid),
            "--depth",
            depth,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=per_paper_timeout,
                cwd=str(PROJECT_ROOT),
            )
            if result.returncode == 0:
                logger.info(f"  Deep-analyzed report {rid}")
            else:
                logger.warning(
                    f"  Deep analysis failed for report {rid}: {result.stderr[:300]}"
                )
        except subprocess.TimeoutExpired:
            logger.warning(
                f"  Deep analysis timed out for report {rid} (>{per_paper_timeout}s)"
            )
        except Exception as e:
            logger.warning(f"  Deep analysis error for report {rid}: {e}")


# ═══════════════════════════════════════════════════
# Step 3: Claude Code analysis
# ═══════════════════════════════════════════════════


def analyze_with_claude(papers: list[dict], date_label: str = "") -> str:
    """Compatibility entrypoint: render saved assessments without a second model call."""
    return render_briefing(papers, date_label)


def fallback_briefing(papers: list[dict], date_label: str = "") -> str:
    """Fallback briefing when Claude Code is unavailable."""
    label = date_label or datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"### 📊 概览（{label}）",
        f"本期选入 {len(papers)} 篇库中论文\n",
    ]
    # Group by source
    sources = {}
    for p in papers:
        src = p.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1
    for src, count in sources.items():
        lines.append(f"- {src}: {count} 篇")

    lines.append("\n### 📄 论文速览\n")
    for p in papers:
        title = p["title"][:80]
        url = p.get("ui_url", "")
        source = p.get("source", "")
        score = p.get("selection_score", 0)
        score_str = f" ⭐{score:.1f}" if score > 0 else ""
        lines.append(f"- **{title}** ({source}){score_str} [📄 查看]({url})")

    return "\n".join(lines)


def ensure_paper_links(briefing: str, papers: list[dict]) -> str:
    """The model may omit links; delivery completeness must not depend on it."""
    missing = [
        p for p in papers if p.get("ui_url") and f"({p['ui_url']})" not in briefing
    ]
    if missing:
        briefing += "\n\n### 论文链接\n" + "\n".join(
            f"- {p['title'][:80]} [📄 查看]({p['ui_url']})" for p in missing
        )
    return briefing


def send_complete_text(receive_id: str, text: str, receive_type: str) -> bool:
    """Send every byte-bounded part and preserve the public text-send adapter."""
    from reportagent.services.feishu_transport import send_complete_text as send_parts

    return send_parts(
        text, lambda part: send_feishu_text(receive_id, part, receive_type)
    )


# ═══════════════════════════════════════════════════
# Step 4: Send Feishu message
# ═══════════════════════════════════════════════════


def send_feishu_text(receive_id: str, text: str, receive_type: str = "open_id"):
    """Boolean compatibility adapter for the shared SDK delivery primitive."""
    from reportagent.services.feishu_transport import (
        build_client,
        send_payload,
        send_complete_text as send_parts,
    )

    try:
        client = build_client()
        return send_parts(
            text,
            lambda part: send_payload(
                client,
                "text",
                json.dumps({"text": part}, ensure_ascii=False),
                receive_id,
                receive_type=receive_type,
            ),
        )
    except Exception:
        logger.warning("Feishu text delivery failed")
        return False


def send_feishu_card(
    receive_id: str,
    briefing: str,
    papers: list[dict],
    date_label: str = "",
    receive_type: str = "open_id",
):
    """Send an interactive card (rich format) to Feishu."""

    label = date_label or datetime.now().strftime("%Y-%m-%d")
    briefing = ensure_paper_links(briefing, papers)

    # Build card elements from briefing + paper links
    elements = []

    # Header
    elements.append(
        {
            "tag": "markdown",
            "content": f"**📊 量化研报日报 — {label}**\n本期选入 {len(papers)} 篇库中论文",
        }
    )

    # Native section breaks keep lists and the next heading visually separate.
    elements.extend(card_sections(briefing, omit_leading_title=True))

    # Divider + action note
    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "基于已保存评估生成 · 原文证据与待核验项见论文详情 | AlphaReport",
                }
            ],
        }
    )

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📊 AlphaReport 日报 · {label}"},
            "template": "wathet",  # Blue theme
        },
        "elements": elements,
    }

    from reportagent.services.feishu_transport import (
        build_client,
        send_card_with_fallback,
    )

    try:
        client = build_client()
        return send_card_with_fallback(
            client,
            card,
            briefing,
            receive_id,
            receive_type=receive_type,
            text_sender=lambda part: send_feishu_text(receive_id, part, receive_type),
        )
    except Exception:
        logger.warning("Feishu daily card delivery failed")
        return False


# ═══════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════


@contextmanager
def daily_run_lock():
    path = PROJECT_ROOT / "data" / "daily-report.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有日报任务正在运行，请等待完成后重试。") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def save_run_record(result: dict) -> None:
    from reportagent.models.database import ReportRun

    init_db()
    with get_session_factory()() as session:
        session.merge(
            ReportRun(
                run_id=result["run_id"],
                status=result["status"],
                result_json=json.dumps(result, ensure_ascii=False),
            )
        )
        session.commit()


def run_daily_push(options: DailyPushOptions) -> dict:
    """One recorded workflow; analysis always precedes delivery."""
    resolve_review_budget(options.max_reviews)
    with daily_run_lock():
        result = {
            "run_id": str(uuid.uuid4()),
            "status": "running",
            "source_statuses": [],
            "report_ids": [],
            "deliveries": {},
            "stages": {},
            "warnings": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        artifact = (
            PROJECT_ROOT / "data" / "daily_reports" / (result["run_id"] + ".json")
        )
        result["run_artifact_path"] = str(artifact)
        try:
            result.update(_prepare_and_deliver(options, result))
            return result
        except Exception as exc:
            result.update(status="failed", error=type(exc).__name__)
            raise
        finally:
            result["completed_at"] = datetime.now(timezone.utc).isoformat()
            try:
                save_run_record(result)
            except Exception as exc:
                result.update(status="failed", record_error=type(exc).__name__)
                raise
            finally:
                # Final run state, including early failures and send results.
                artifact.parent.mkdir(parents=True, exist_ok=True)
                temporary = artifact.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                temporary.replace(artifact)


def _prepare_and_deliver(options: DailyPushOptions, run: dict) -> dict:
    global UI_BASE_URL, PUSH_SETTINGS
    PUSH_SETTINGS = _load_push_settings()
    max_reviews = resolve_review_budget(options.max_reviews, PUSH_SETTINGS)
    run["review_budget"] = max_reviews
    _, _, report_date, _ = get_daily_range()
    run["report_date"] = report_date
    max_featured = min(5, int(PUSH_SETTINGS.get("max_featured", 5)))
    # Preview must not rewrite .env or discover a new public tunnel.
    if not options.dry_run:
        try:
            sync_public_url()
        except (OSError, UnicodeError):
            logger.warning("Public URL sync skipped; using configured URL")
        load_dotenv(override=True)
    UI_BASE_URL = get_web_base_url()
    receive_ids = [
        s.strip()
        for s in (
            options.receive_ids or os.environ.get("FEISHU_DAILY_PUSH_IDS", "")
        ).split(",")
        if s.strip()
    ]
    receive_type = options.receive_type or os.environ.get(
        "FEISHU_DAILY_PUSH_TYPE", "open_id"
    )
    if not options.dry_run and not receive_ids:
        raise ValueError("请明确指定飞书收件人")
    collection = read_latest_collection(PROJECT_ROOT)
    source_statuses = collection.get("source_statuses", []) if collection else []
    checked_at = collection["completed_at"] if collection else None
    run["stages"]["collection"] = {
        "status": "not_run",
        "reason": "report_reads_library_only",
    }
    run["collection_run"] = collection
    if collection and collection["status"] in {"failed", "partial", "cancelled"}:
        run["warnings"].append(
            "最近一次定时采集未完整成功；本次仅使用库中已有资料，最新资料覆盖可能不完整。"
        )
    ids = [int(s.strip()) for s in options.report_ids.split(",") if s.strip()]
    run["stages"]["relevance"] = {"status": "skipped", "reason": "explicit_report_ids"}
    if not ids:
        # Apply the current rubric to unjudged/stale library rows.
        try:
            scored = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "reportagent.cli.llm_filter",
                    "--created-date",
                    report_date,
                    "--max-papers",
                    "0",
                    "--batch-size",
                    str(PUSH_SETTINGS.get("llm_batch_size", 10)),
                ],
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
            )
            run["stages"]["relevance"] = {
                "status": "failed" if scored.returncode else "ok",
                "returncode": scored.returncode,
            }
        except (subprocess.SubprocessError, OSError) as exc:
            run["stages"]["relevance"] = {
                "status": "failed",
                "error": type(exc).__name__,
            }
        if run["stages"]["relevance"]["status"] == "failed":
            warning = "相关性筛选未完整成功；本期只包含已通过当前筛选标准的有效候选，覆盖可能不完整。"
            run["warnings"].append(warning)
            logger.warning(warning)
    papers, date_label, _ = get_todays_papers(
        report_ids=ids or None, report_date=report_date
    )
    if ids and not papers:
        raise ValueError("指定论文不存在")
    candidates = select_candidates(papers, len(papers), list(load_enabled_directions()))
    run["report_ids"] = [p["id"] for p in candidates]
    if not candidates and run["stages"]["relevance"]["status"] == "failed":
        raise RuntimeError("相关性筛选失败且没有可用候选，本次不能作为正常空日报。")
    review_audit = {}
    assessments = (
        asyncio.run(
            review_report_ids(
                run["report_ids"],
                force=options.force_review,
                max_reviews=max_reviews,
                audit=review_audit,
            )
        )
        if candidates
        else {}
    )
    run["stages"]["review"] = review_audit
    candidates = [p for p in candidates if p["id"] in assessments]
    run["report_ids"] = [p["id"] for p in candidates]
    for p in candidates:
        p["evaluation"] = assessments.get(p["id"], pending_review(p, "全文评估未完成"))
    candidates = _refresh_candidate_metadata(candidates)
    run["papers"] = candidates

    def render(papers):
        text = render_briefing(
            papers,
            date_label,
            source_statuses=source_statuses,
            max_featured=max_featured,
            source_checked_at=checked_at,
        )
        deferred = len(review_audit.get("deferred_ids", []))
        if deferred:
            text += f"\n\n本次全文评估预算为 {max_reviews} 篇，另有 {deferred} 篇候选尚未评估；重跑可继续处理。"
        if ids:
            text += "\n\n本次为指定论文评估，未按当日新增入库范围筛选。"
        if not papers:
            text += "\n\n当日暂无通过当前相关性筛选的新增入库论文。"
        if run["warnings"]:
            text = (
                "\n".join(f"> ⚠ {warning}" for warning in run["warnings"])
                + "\n\n"
                + text
            )
        return text

    briefing = render(candidates)
    directory = PROJECT_ROOT / "data" / "daily_reports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (run["run_id"] + ".md")
    path.write_text(briefing, encoding="utf-8")
    if options.output:
        Path(options.output).write_text(briefing, encoding="utf-8")
    run.update(
        artifact_path=str(path),
        paper_count=len(candidates),
        source_statuses=source_statuses,
        briefing=briefing,
    )
    if options.dry_run:
        print(briefing)
        return {
            "status": "partial" if run["warnings"] else "preview",
            "outcome": "preview",
            "artifact_path": str(path),
        }
    any_failure = False
    for recipient in dict.fromkeys(receive_ids):
        success = send_feishu_card(
            recipient, briefing, candidates, date_label, receive_type
        )
        run["deliveries"][recipient] = success
        if not success:
            any_failure = True
    if any_failure:
        raise RuntimeError("部分飞书投递未成功，请查看本次运行中的逐收件人发送结果")
    status = (
        "empty" if not candidates else "partial" if run["warnings"] else "delivered"
    )
    return {"status": status, "outcome": "delivered", "artifact_path": str(path)}
