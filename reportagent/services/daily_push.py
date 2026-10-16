"""Coordinate daily collection, selection, briefing and recipient-specific delivery."""

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
from datetime import datetime, timezone, timedelta
from pathlib import Path


from dotenv import load_dotenv
load_dotenv()

from reportagent.db.engine import init_db, get_session_factory
from reportagent.utils.config import get_config, PROJECT_ROOT
from reportagent.services.public_url import sync_public_url
from reportagent.skills.registry import skill_resource
from reportagent.services.report_quality import review_report_ids, select_candidates, pending_review, review_fingerprint
from reportagent.services.report_briefing import render_briefing
from reportagent.services.feishu_markdown import card_sections
from reportagent.services.paper_scoring import load_enabled_directions, scoring_fingerprint, screening_is_current

logger = logging.getLogger("daily_push")

# ── Config ──
cfg = get_config("feishu", default={})
APP_ID = os.environ.get(cfg.get("app_id_env", "FEISHU_APP_ID"), "")
APP_SECRET = os.environ.get(cfg.get("app_secret_env", "FEISHU_APP_SECRET"), "")

# UI base URL for generating paper links
SERVER_HOST = os.environ.get("ALPHAREPORT_HOST", "localhost")
SERVER_PORT = os.environ.get("ALPHAREPORT_PORT", "8000")
UI_BASE_URL = os.environ.get("ALPHAREPORT_BASE_URL", f"http://{SERVER_HOST}:{SERVER_PORT}")

# Feishu receive targets — comma-separated open_ids or chat_ids
FEISHU_RECEIVE_IDS = os.environ.get("FEISHU_DAILY_PUSH_IDS", "").split(",") if os.environ.get("FEISHU_DAILY_PUSH_IDS") else []
FEISHU_RECEIVE_TYPE = os.environ.get("FEISHU_DAILY_PUSH_TYPE", "open_id")  # "open_id" | "chat_id"

# ── Load push directions from config ────────────────────
@dataclass(frozen=True)
class DailyPushOptions:
    topics: str | None = None
    receive_ids: str = ''
    receive_type: str = ''
    dry_run: bool = False
    no_collect: bool = False
    max_per_source: int = 15
    output: str | None = None
    report_ids: str = ""
    max_reviews: int | None = None
    force_review: bool = False


def _load_push_directions() -> list[dict]:
    """Load push direction definitions from configs/push_directions.yaml."""
    import yaml
    directions_path = PROJECT_ROOT / "configs" / "push_directions.yaml"
    if directions_path.exists():
        with open(directions_path) as f:
            data = yaml.safe_load(f)
            return [d for d in data.get("directions", []) if d.get("enabled", True)]
    return []

PUSH_DIRECTIONS = _load_push_directions()

# Build aggregated topics from enabled directions — use high-level topic names
# that expand to specific finance English terms via topic_expansion.py
def _build_default_topics() -> list[str]:
    """Build topic list from push directions for arXiv search."""
    # Use the top-level topic mapping keys (mapped to finance-specific English terms)
    # These are the topic names from push_directions that match topic_expansion.py entries
    topics = [
        "量化策略", "因子模型", "高频交易", "风险模型",
        "执行算法", "组合优化", "统计套利", "波动率", "另类数据",
    ]
    # Also include some English-only topics from topic_expansion
    extra_en = [
        "market microstructure", "factor investing", "portfolio optimization",
    ]
    return topics + extra_en

DEFAULT_TOPICS = _build_default_topics()
PUSH_SETTINGS: dict = {}
if PUSH_DIRECTIONS:
    import yaml as _yaml
    with open(PROJECT_ROOT / "configs" / "push_directions.yaml") as _f:
        _cfg = _yaml.safe_load(_f)
        PUSH_SETTINGS = _cfg.get("push_settings", {})


# ═══════════════════════════════════════════════════
# Step 1: Collect papers
# ═══════════════════════════════════════════════════

def collect_papers(topics: list[str], max_per_source: int = 15, broad: bool = True) -> dict:
    """Run the collection CLI in a bounded process and return its JSON result.

    Args:
        topics: Search topics for keyword-based collection (used as fallback / supplementary)
        max_per_source: Max papers per source
        broad: If True, use --broad mode for full pull (全量拉取) — pulls ALL q-fin papers
               without keyword restrictions. LLM filtering happens separately after collection.
    """
    topics_str = ",".join(topics)
    cmd = [
        sys.executable, "-m", "reportagent.cli.collect",
        "--topics", topics_str,
        "--sources", "arxiv,eastmoney,bigquant",
        "--max", str(max_per_source),
        "--days", str(PUSH_SETTINGS.get("lookback_days", 2)),
        # 深度分析（standard depth）对每篇论文要跑 5~10 轮 LLM 调用，单篇就要几分钟。
        # 全量拉取模式下常一次抓到 10+ 篇候选，多数在精选阶段就会被刷掉——先对全部
        # 候选做深度分析纯属浪费，还会把 15 分钟的采集窗口耗光导致超时。
        # 这里只做分类入库（快），深度分析挪到精选之后、只对入选论文做（见 deep_analyze_selected）。
        "--no-analysis",
    ]
    if broad:
        cmd.append("--broad")
        logger.info("全量拉取模式 (Broad mode): 拉取所有 q-fin 论文，不做关键词限制")

    logger.info(f"Collecting: {cmd}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=str(PROJECT_ROOT))
        if result.returncode == 0:
            # Parse JSON from stdout — find the line that starts with {"topics":
            stdout = result.stdout.strip()
            data = None
            for line in stdout.split("\n"):
                line = line.strip()
                if line.startswith('{"topics"'):
                    try:
                        data = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            if data is None:
                # Fallback: try last line
                try:
                    data = json.loads(stdout.split("\n")[-1])
                except (json.JSONDecodeError, IndexError):
                    data = {}
            logger.info(f"Collection done: {data.get('storage', {})}")
            return data
        else:
            logger.error(f"Collect failed: {result.stderr[:500]}")
            return {"error": result.stderr[:500]}
    except subprocess.TimeoutExpired:
        logger.error("Collect timed out")
        return {"error": "timeout"}
    except Exception as e:
        logger.error(f"Collect error: {e}")
        return {"error": str(e)}


# ═══════════════════════════════════════════════════
# Step 2: Query recent papers (rolling window)
# ═══════════════════════════════════════════════════

# arXiv 入库比 published_date 滞后 1-3 天，2 天窗口几乎永远为空 → 放宽到 4 天
PUSH_WINDOW_DAYS = 4


def _paper_metadata(row) -> dict:
    """Snapshot only the report metadata used by daily rendering and delivery."""
    return {
        "id": row.id,
        **{field: getattr(row, field) or "" for field in (
            "title", "authors", "abstract", "source", "source_url", "arxiv_id", "doi", "topics",
            "selection_reason",
        )},
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
        current = {row.id: row for row in session.query(Report).filter(
            Report.id.in_([paper["id"] for paper in candidates]),
        ).all()}
        refreshed = []
        for paper in candidates:
            row = current.get(paper["id"])
            if row is None:
                refreshed.append({**paper, "evaluation": pending_review(paper, "论文记录已删除，请重新获取并评估。")})
                continue
            snapshot = {field: getattr(row, field) for field in (
                "source", "arxiv_id", "title", "abstract", "full_text", "selection_relevance",
                "selection_score", "selection_reason", "selection_provenance_json",
            )}
            try:
                saved = json.loads(row.evaluation_json or "null")
            except (TypeError, ValueError):
                saved = None
            matches = isinstance(saved, dict) and saved == paper.get("evaluation")
            if matches and (saved.get("status") == "reviewed" or saved.get("fingerprint")):
                model = saved.get("model")
                matches = (isinstance(model, str) and bool(model)
                           and saved.get("fingerprint") == review_fingerprint(snapshot, model))
            evaluation = saved if matches else pending_review(
                snapshot, "准备日报期间原文或已保存评估已更新，请重新评估。")
            refreshed.append({**paper, **_paper_metadata(row), "ui_url": paper["ui_url"],
                              "evaluation": evaluation})
        return refreshed


def get_todays_papers(collect_result: dict = None, receive_ids: list[str] | None = None,
                     receive_type: str = "open_id", report_ids: list[int] | None = None) -> tuple[list[dict], str, str]:
    """Get LLM-selected, not-yet-pushed papers for the daily push.

    策略：取 published_date 落在最近 PUSH_WINDOW_DAYS 天（含今天）、is_selected=1
    且至少一位目标收件人尚未收到的论文。未指定目标时沿用旧的 pushed_at 查询。
    使用 published_date（发表日期），非 created_at（入库日期）。

    Returns:
        (papers, date_label_start, date_label_end) — date labels for briefing title
    """
    init_db()
    factory = get_session_factory()
    session = factory()
    try:
        from reportagent.models.database import Report, ReportDelivery
        from sqlalchemy import desc, exists, or_

        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start.replace(hour=23, minute=59, second=59, microsecond=999999)
        window_start = today_start - timedelta(days=PUSH_WINDOW_DAYS - 1)

        logger.info(f"Query window: published_date {window_start.strftime('%Y-%m-%d')} "
                    f"~ {today_end.strftime('%Y-%m-%d')} "
                    f"({PUSH_WINDOW_DAYS} days), is_selected=1, pending delivery")

        q = session.query(Report)
        if report_ids:
            q = q.filter(Report.id.in_(report_ids))
        else:
            q = q.filter(Report.published_date >= window_start, Report.published_date <= today_end,
                         Report.selection_relevance >= PUSH_SETTINGS.get("llm_filter_threshold", 6.0))
        if receive_ids:
            # A global pushed_at flag cannot tell whether every recipient received it.
            q = q.filter(or_(*[
                ~exists().where(ReportDelivery.report_id == Report.id,
                                ReportDelivery.receive_type == receive_type,
                                ReportDelivery.receive_id == rid)
                for rid in receive_ids
            ]))
        else:
            q = q.filter(Report.pushed_at.is_(None))
        q = q.order_by(desc(Report.selection_relevance), desc(Report.published_date))
        reports = q.all()
        if not report_ids:
            reports = [r for r in reports if screening_is_current(r)][:200]

        if not reports:
            date_label = today_start.strftime("%Y-%m-%d")
            logger.warning(
                f"No selected & unpushed papers in the last {PUSH_WINDOW_DAYS} days "
                f"({window_start.strftime('%Y-%m-%d')} ~ {today_start.strftime('%Y-%m-%d')}). "
                f"Sending empty brief."
            )
            return [], date_label, date_label

        # 实际取到了哪几天的论文
        day_counts: dict[str, int] = {}
        for r in reports:
            day = r.published_date.strftime("%Y-%m-%d") if r.published_date else "unknown"
            day_counts[day] = day_counts.get(day, 0) + 1
        days_sorted = sorted(d for d in day_counts if d != "unknown")
        logger.info("Papers by published_date: "
                    + ", ".join(f"{d}: {day_counts[d]}" for d in sorted(day_counts, reverse=True)))

        if days_sorted:
            label_start, label_end = days_sorted[0], days_sorted[-1]
        else:
            label_start = label_end = today_start.strftime("%Y-%m-%d")
        date_label = label_end if label_start == label_end else f"{label_start} ~ {label_end}"

        papers = [_paper_metadata(row) for row in reports]

        selection_count = sum(1 for p in papers if p.get("selection_score", 0) > 0)
        logger.info(f"Found {len(papers)} papers for push ({selection_count} LLM-selected), date label: {date_label}")
        return papers, date_label, date_label
    finally:
        session.close()


def pending_for_recipient(papers: list[dict], receive_id: str, receive_type: str) -> list[dict]:
    from reportagent.models.database import ReportDelivery
    with get_session_factory()() as session:
        delivered = {row[0] for row in session.query(ReportDelivery.report_id).filter(
            ReportDelivery.receive_id == receive_id, ReportDelivery.receive_type == receive_type,
            ReportDelivery.report_id.in_([p["id"] for p in papers]),
        ).all()}
    return [p for p in papers if p["id"] not in delivered]


def mark_papers_pushed(paper_ids: list[int], receive_id: str, receive_type: str) -> int:
    """Record successful delivery to one destination, retaining pushed_at for legacy readers."""
    if not paper_ids:
        return 0
    init_db()
    factory = get_session_factory()
    session = factory()
    try:
        from reportagent.models.database import Report, ReportDelivery
        from sqlalchemy.dialects.sqlite import insert

        stamp = datetime.now(timezone.utc).isoformat()
        session.execute(insert(ReportDelivery).values([
            {"report_id": rid, "receive_id": receive_id, "receive_type": receive_type}
            for rid in paper_ids
        ]).on_conflict_do_nothing())
        updated = (
            session.query(Report)
            .filter(Report.id.in_(paper_ids))
            .update({Report.pushed_at: stamp}, synchronize_session=False)
        )
        session.commit()
        logger.info(f"Marked {updated} papers as pushed (pushed_at={stamp})")
        return updated
    except Exception as e:
        session.rollback()
        logger.error(f"Failed to mark pushed_at: {e}")
        raise
    finally:
        session.close()


def deep_analyze_selected(paper_ids: list[int], depth: str = "standard", per_paper_timeout: int = 240) -> None:
    """Run full-depth analysis (analyze.py --report-id) for only the papers that made
    it through selection and got pushed. This runs AFTER the push so it never delays
    delivery, and only touches the handful of selected papers instead of every
    collected candidate — most candidates get rejected at selection anyway, so deep
    analysis on the whole batch is wasted work (this is what caused the daily collect
    step to blow past its 15-min timeout on 2026-07-29/30).
    """
    for rid in paper_ids:
        cmd = [
            sys.executable, "-m", "reportagent.cli.analyze",
            "--report-id", str(rid), "--depth", depth,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=per_paper_timeout,
                cwd=str(PROJECT_ROOT),
            )
            if result.returncode == 0:
                logger.info(f"  Deep-analyzed report {rid}")
            else:
                logger.warning(f"  Deep analysis failed for report {rid}: {result.stderr[:300]}")
        except subprocess.TimeoutExpired:
            logger.warning(f"  Deep analysis timed out for report {rid} (>{per_paper_timeout}s)")
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
        f"共收录 {len(papers)} 篇量化金融精选论文\n",
    ]
    # Group by source
    sources = {}
    for p in papers:
        src = p.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1
    for src, count in sources.items():
        lines.append(f"- {src}: {count} 篇")

    lines.append(f"\n### 📄 论文速览\n")
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
    missing = [p for p in papers if p.get("ui_url") and f"({p['ui_url']})" not in briefing]
    if missing:
        briefing += "\n\n### 论文链接\n" + "\n".join(
            f"- {p['title'][:80]} [📄 查看]({p['ui_url']})" for p in missing)
    return briefing


def send_complete_text(receive_id: str, text: str, receive_type: str) -> bool:
    """Send every part, and report failure if any part was rejected."""
    remaining = text
    while remaining:
        boundary = 3000
        if len(remaining) > boundary:
            split = remaining.rfind("\n", 0, boundary)
            if split > 0:
                boundary = split + 1
        if not send_feishu_text(receive_id, remaining[:boundary], receive_type):
            return False
        remaining = remaining[boundary:]
    return True


# ═══════════════════════════════════════════════════
# Step 4: Send Feishu message
# ═══════════════════════════════════════════════════

def send_feishu_text(receive_id: str, text: str, receive_type: str = "open_id"):
    """Send a text message via Feishu API."""
    import lark_oapi as lark
    from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
    from lark_oapi.api.im.v1.model.create_message_request_body import CreateMessageRequestBody

    client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
    content = json.dumps({"text": text}, ensure_ascii=False)
    request = CreateMessageRequest.builder() \
        .receive_id_type(receive_type) \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("text")
            .content(content)
            .build()
        ).build()
    try:
        resp = client.im.v1.message.create(request)
        if resp.success():
            logger.info(f"Sent to {receive_id}")
            return True
        else:
            logger.error(f"Send failed: {resp.code} {resp.msg}")
            return False
    except Exception as e:
        logger.error(f"Send error: {e}")
        return False


def send_feishu_card(receive_id: str, briefing: str, papers: list[dict], date_label: str = "", receive_type: str = "open_id"):
    """Send an interactive card (rich format) to Feishu."""

    label = date_label or datetime.now().strftime("%Y-%m-%d")
    briefing = ensure_paper_links(briefing, papers)

    # Build card elements from briefing + paper links
    elements = []

    # Header
    elements.append({
        "tag": "markdown",
        "content": f"**📊 量化研报日报 — {label}**\n共收录 {len(papers)} 篇论文"
    })

    # Native section breaks keep lists and the next heading visually separate.
    elements.extend(card_sections(briefing, omit_leading_title=True))

    # Divider + action note
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "基于已保存评估生成 · 原文证据与待核验项见论文详情 | AlphaReport"}]
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📊 AlphaReport 日报 · {label}"},
            "template": "wathet",  # Blue theme
        },
        "elements": elements,
    }

    import lark_oapi as lark
    from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
    from lark_oapi.api.im.v1.model.create_message_request_body import CreateMessageRequestBody

    client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
    card_json = json.dumps(card, ensure_ascii=False)
    request = CreateMessageRequest.builder() \
        .receive_id_type(receive_type) \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("interactive")
            .content(card_json)
            .build()
        ).build()
    try:
        resp = client.im.v1.message.create(request)
        if resp.success():
            logger.info(f"Card sent to {receive_id}")
            return True
        else:
            logger.error(f"Card send failed: {resp.code} {resp.msg}")
            # Fall back to text
            return send_complete_text(receive_id, briefing, receive_type)
    except Exception as e:
        logger.error(f"Card send error: {e}")
        return send_complete_text(receive_id, briefing, receive_type)


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
        session.merge(ReportRun(run_id=result["run_id"], status=result["status"],
                                result_json=json.dumps(result, ensure_ascii=False)))
        session.commit()


def run_daily_push(options: DailyPushOptions) -> dict:
    """One bounded, recorded workflow; analysis always precedes delivery."""
    if options.max_reviews is not None and (type(options.max_reviews) is not int or not 1 <= options.max_reviews <= 12):
        raise ValueError("每期全文评估数量必须在 1 到 12 之间")
    if options.report_ids and not options.no_collect:
        raise ValueError("指定论文请同时使用 --no-collect，避免混入自动采集结果")
    with daily_run_lock():
        result = {"run_id": str(uuid.uuid4()), "status": "running", "source_statuses": [],
                  "report_ids": [], "deliveries": {}, "stages": {}, "warnings": [],
                  "started_at": datetime.now(timezone.utc).isoformat()}
        artifact = PROJECT_ROOT / "data" / "daily_reports" / (result["run_id"] + ".json")
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
                # This is the final run state, including early failures and delivery receipts.
                artifact.parent.mkdir(parents=True, exist_ok=True)
                temporary = artifact.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(artifact)


def _prepare_and_deliver(options: DailyPushOptions, run: dict) -> dict:
    global UI_BASE_URL, PUSH_SETTINGS
    import yaml
    with (PROJECT_ROOT / "configs" / "push_directions.yaml").open() as handle:
        PUSH_SETTINGS = yaml.safe_load(handle).get("push_settings", {})
    max_reviews = options.max_reviews if options.max_reviews is not None else PUSH_SETTINGS.get("max_reviews", 12)
    if type(max_reviews) is not int or not 1 <= max_reviews <= 12:
        raise ValueError("每期全文评估数量必须在 1 到 12 之间")
    max_featured = min(5, int(PUSH_SETTINGS.get("max_featured", 5)))
    # Preview must not rewrite .env or discover a new public tunnel.
    if not options.dry_run:
        try:
            sync_public_url()
        except (OSError, UnicodeError):
            logger.warning("Public URL sync skipped; using configured URL")
        load_dotenv(override=True)
    UI_BASE_URL = os.environ.get("ALPHAREPORT_BASE_URL", f"http://{SERVER_HOST}:{SERVER_PORT}")
    receive_ids = [s.strip() for s in (options.receive_ids or os.environ.get("FEISHU_DAILY_PUSH_IDS", "")).split(",") if s.strip()]
    receive_type = options.receive_type or os.environ.get("FEISHU_DAILY_PUSH_TYPE", "open_id")
    if not options.dry_run and not receive_ids:
        raise ValueError("请明确指定飞书收件人")
    source_statuses = []
    run["stages"]["collection"] = {"status": "skipped"}
    if not options.no_collect:
        topics = [s.strip() for s in (options.topics or ",".join(DEFAULT_TOPICS)).split(",") if s.strip()]
        try:
            collection = collect_papers(topics, options.max_per_source, broad=True)
        except Exception as exc:
            run["stages"]["collection"] = {"status": "failed", "error": type(exc).__name__}
            raise
        source_statuses = collection.get("source_statuses", [])
        run["source_statuses"] = source_statuses
        if collection.get("status") == "failed" or "error" in collection or not source_statuses:
            run["stages"]["collection"] = {"status": "failed"}
            raise RuntimeError("来源采集失败或未返回可核验状态，本次不生成正常日报。")
        run["stages"]["collection"] = {"status": collection.get("status", "ok")}
    ids = [int(s.strip()) for s in options.report_ids.split(",") if s.strip()]
    run["stages"]["relevance"] = {"status": "skipped", "reason": "explicit_report_ids"}
    if not ids:
        # no-collect still applies the current rubric to unjudged/stale library rows.
        try:
            scored = subprocess.run([sys.executable, "-m", "reportagent.cli.llm_filter", "--published-days", str(PUSH_WINDOW_DAYS),
                                     "--batch-size", str(PUSH_SETTINGS.get("llm_batch_size", 10))],
                                    capture_output=True, text=True, timeout=300, cwd=str(PROJECT_ROOT))
            run["stages"]["relevance"] = {
                "status": "failed" if scored.returncode else "ok", "returncode": scored.returncode,
            }
        except (subprocess.SubprocessError, OSError) as exc:
            run["stages"]["relevance"] = {"status": "failed", "error": type(exc).__name__}
        if run["stages"]["relevance"]["status"] == "failed":
            warning = "相关性筛选未完整成功；本期只包含已通过当前筛选标准的有效候选，覆盖可能不完整。"
            run["warnings"].append(warning)
            logger.warning(warning)
    papers, date_label, _ = get_todays_papers(receive_ids=receive_ids or None, receive_type=receive_type, report_ids=ids or None)
    if ids and not papers:
        raise ValueError("指定论文不存在或已送达目标会话")
    candidates = select_candidates(papers, max_reviews, list(load_enabled_directions()))
    run["report_ids"] = [p["id"] for p in candidates]
    if not candidates and run["stages"]["relevance"]["status"] == "failed":
        raise RuntimeError("相关性筛选失败且没有可用候选，本次不能作为正常空日报。")
    assessments = asyncio.run(review_report_ids(run["report_ids"], force=options.force_review)) if candidates else {}
    for p in candidates:
        p["evaluation"] = assessments.get(p["id"], pending_review(p, "全文评估未完成"))
    candidates = _refresh_candidate_metadata(candidates)
    run["papers"] = candidates

    def render(papers):
        text = render_briefing(papers, date_label, source_statuses=source_statuses, max_featured=max_featured)
        if run["warnings"]:
            text = "\n".join(f"> ⚠ {warning}" for warning in run["warnings"]) + "\n\n" + text
        return text

    briefing = render(candidates)
    directory = PROJECT_ROOT / "data" / "daily_reports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (run["run_id"] + ".md")
    path.write_text(briefing, encoding="utf-8")
    if options.output:
        Path(options.output).write_text(briefing, encoding="utf-8")
    run.update(artifact_path=str(path), paper_count=len(candidates), source_statuses=source_statuses, briefing=briefing)
    if options.dry_run:
        print(briefing)
        return {"status": "partial" if run["warnings"] else "preview", "outcome": "preview", "artifact_path": str(path)}
    if not candidates:
        return {"status": "empty", "artifact_path": str(path)}
    any_failure = False
    for recipient in dict.fromkeys(receive_ids):
        pending = pending_for_recipient(candidates, recipient, receive_type)
        if not pending:
            continue
        recipient_brief = render(pending)
        success = send_feishu_card(recipient, recipient_brief, pending, date_label, receive_type)
        run["deliveries"][recipient] = success
        if success:
            # Failed/partial review remains eligible for future re-evaluation.
            finished = [p["id"] for p in pending if p["evaluation"].get("status") == "reviewed"]
            if finished:
                mark_papers_pushed(finished, recipient, receive_type)
        else:
            any_failure = True
    if any_failure:
        raise RuntimeError("部分飞书投递未成功，已保留成功收件人的回执")
    return {"status": "partial" if run["warnings"] else "delivered", "outcome": "delivered", "artifact_path": str(path)}
