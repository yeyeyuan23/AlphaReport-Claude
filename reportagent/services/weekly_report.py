"""Query the previous publication week, generate its report and deliver it."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path


from dotenv import load_dotenv
load_dotenv()

from reportagent.db.engine import init_db, get_session_factory
from reportagent.utils.config import get_config, PROJECT_ROOT
from reportagent.skills.registry import skill_resource

logger = logging.getLogger("weekly_report")

# ── Config ──
cfg = get_config("feishu", default={})
APP_ID = os.environ.get(cfg.get("app_id_env", "FEISHU_APP_ID"), "")
APP_SECRET = os.environ.get(cfg.get("app_secret_env", "FEISHU_APP_SECRET"), "")

SERVER_HOST = os.environ.get("ALPHAREPORT_HOST", "localhost")
SERVER_PORT = os.environ.get("ALPHAREPORT_PORT", "8000")
UI_BASE_URL = os.environ.get("ALPHAREPORT_BASE_URL", f"http://{SERVER_HOST}:{SERVER_PORT}")

FEISHU_RECEIVE_IDS = os.environ.get("FEISHU_WEEKLY_PUSH_IDS", "").split(",") if os.environ.get("FEISHU_WEEKLY_PUSH_IDS") else []
FEISHU_RECEIVE_TYPE = os.environ.get("FEISHU_WEEKLY_PUSH_TYPE", "chat_id")

# Fallback to daily push config if weekly not set
if not FEISHU_RECEIVE_IDS or FEISHU_RECEIVE_IDS == [""]:
    FEISHU_RECEIVE_IDS = os.environ.get("FEISHU_DAILY_PUSH_IDS", "").split(",") if os.environ.get("FEISHU_DAILY_PUSH_IDS") else []
    FEISHU_RECEIVE_IDS = [i for i in FEISHU_RECEIVE_IDS if i]
if not FEISHU_RECEIVE_IDS or FEISHU_RECEIVE_IDS == [""]:
    FEISHU_RECEIVE_TYPE = os.environ.get("FEISHU_DAILY_PUSH_TYPE", "chat_id")


# ═══════════════════════════════════════════════════
# Date helpers — all based on published_date
# ═══════════════════════════════════════════════════

@dataclass(frozen=True)
class WeeklyReportOptions:
    dry_run: bool = False
    weeks_ago: int = 0
    receive_ids: str = ''
    receive_type: str = ''
    output: str | None = None
    no_llm: bool = False


def get_previous_week_range(weeks_ago: int = 0) -> tuple[datetime, datetime, str, str]:
    """
    计算上周（Monday 00:00 ~ Sunday 23:59:59）的 UTC 范围。

    Args:
        weeks_ago: 0 = 上周, 1 = 上上周, 2 = 三周前 ...

    Returns:
        (week_start, week_end, start_label, end_label)
        Labels are date strings like '2026-06-29' for display.
    """
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    # Today's weekday: Monday=0 ... Sunday=6
    weekday = today.weekday()

    # Last Monday (or further back)
    last_monday = today - timedelta(days=weekday + 7 + weeks_ago * 7)
    # Following Sunday
    last_sunday = last_monday + timedelta(days=6)
    # End of Sunday
    week_end = last_sunday.replace(hour=23, minute=59, second=59, microsecond=999999)

    start_label = last_monday.strftime("%Y-%m-%d")
    end_label = last_sunday.strftime("%Y-%m-%d")

    return last_monday, week_end, start_label, end_label


# ═══════════════════════════════════════════════════
# Query papers by published_date
# ═══════════════════════════════════════════════════

def get_weekly_papers(week_start: datetime, week_end: datetime) -> list[dict]:
    """Query papers with published_date in [week_start, week_end]."""
    init_db()
    factory = get_session_factory()
    session = factory()
    try:
        from reportagent.models.database import Report
        from sqlalchemy import desc

        q = session.query(Report).filter(
            Report.published_date >= week_start,
            Report.published_date <= week_end,
        )
        q = q.order_by(desc(Report.published_date)).limit(100)
        reports = q.all()

        papers = []
        for r in reports:
            papers.append({
                "id": r.id,
                "title": r.title or "",
                "authors": r.authors or "",
                "abstract": (r.abstract or "")[:300],
                "source": r.source or "",
                "source_url": r.source_url or "",
                "arxiv_id": r.arxiv_id or "",
                "doi": r.doi or "",
                "topics": r.topics or "",
                "published_date": r.published_date.isoformat() if r.published_date else "",
                "has_full_text": r.has_full_text,
                "selection_score": r.selection_score or 0.0,
                "is_selected": bool(r.is_selected),
                "summary": (r.summary or "")[:200],
                "ui_url": f"{UI_BASE_URL}/library/{r.id}",
            })

        logger.info(f"Found {len(papers)} papers published between "
                    f"{week_start.strftime('%Y-%m-%d')} and {week_end.strftime('%Y-%m-%d')}")
        return papers
    finally:
        session.close()


# ═══════════════════════════════════════════════════
# LLM Weekly Report Generation
# ═══════════════════════════════════════════════════


def generate_weekly_report(papers: list[dict], week_start: str, week_end: str) -> str:
    """Generate weekly report via LLM."""
    import shutil

    claude_bin = shutil.which("claude")
    if not claude_bin:
        logger.error("Claude Code not found")
        return fallback_weekly_report(papers, week_start, week_end)

    # Only send top papers + brief list to LLM to reduce prompt size
    selected = sorted(
        [p for p in papers if p.get("selection_score", 0) >= 6.0],
        key=lambda x: -(x.get("selection_score", 0))
    )[:5]
    rest = [p for p in papers if p not in selected]
    papers_for_llm = selected + rest

    papers_json = json.dumps(papers_for_llm, ensure_ascii=False, indent=2)
    prompt = skill_resource("weekly-report", "briefing.md").read_text(encoding="utf-8").format(
        week_start=week_start,
        week_end=week_end,
        total_count=len(papers),
        papers_json=papers_json,
    )

    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(PROJECT_ROOT / ".claude-bot")

    logger.info("Generating weekly report via Claude Code...")
    try:
        result = subprocess.run(
            [claude_bin, "-p", prompt, "--print", "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=300,  # 5 min for weekly
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        stdout = result.stdout.strip()
        if result.returncode == 0 and stdout:
            logger.info(f"Weekly report generated: {len(stdout)} chars")
            return stdout
        else:
            logger.error(f"Claude failed: {result.stderr[:300]}")
            return fallback_weekly_report(papers, week_start, week_end)
    except subprocess.TimeoutExpired:
        logger.error("Claude timed out")
        return fallback_weekly_report(papers, week_start, week_end)
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return fallback_weekly_report(papers, week_start, week_end)


def fallback_weekly_report(papers: list[dict], week_start: str, week_end: str) -> str:
    """Fallback report without LLM."""
    selected = sorted(
        [p for p in papers if p.get("selection_score", 0) >= 6.0],
        key=lambda x: -(x.get("selection_score", 0))
    )[:5]
    rest = [p for p in papers if p not in selected]

    lines = [
        f"### 📊 AlphaReport 周报",
        f"**{week_start} ~ {week_end}** ｜ 共 {len(papers)} 篇",
        "",
    ]

    if selected:
        lines.append("### 🏆 精选 Top 5\n")
        for p in selected:
            score = p.get("selection_score", 0)
            title = p["title"][:70]
            url = p.get("ui_url", "")
            pub_date = p.get("published_date", "")[:10]
            abstract = (p.get("abstract", "") or "")[:120]
            lines.append(f"**{title}** ⭐{score:.1f} | {pub_date}")
            if abstract:
                lines.append(f">{abstract[:120]}...")
            lines.append(f"[📄 查看]({url})\n")

    if rest:
        lines.append(f"### 📄 其余 {len(rest)} 篇")
        for p in rest[:20]:
            title = p["title"][:60]
            url = p.get("ui_url", "")
            pub_date = p.get("published_date", "")[:10]
            lines.append(f"- {title} ({pub_date}) [📄 查看]({url})")
        if len(rest) > 20:
            lines.append(f"... 还有 {len(rest) - 20} 篇")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════
# Send to Feishu
# ═══════════════════════════════════════════════════

def send_feishu_card(receive_id: str, report_text: str, papers: list[dict],
                     week_start: str, week_end: str, receive_type: str = "chat_id"):
    """Send interactive card to Feishu."""

    elements = []

    # Header
    elements.append({
        "tag": "markdown",
        "content": f"**📊 量化研报周报**\n{week_start} ~ {week_end} ｜ 共收录 {len(papers)} 篇论文"
    })

    # Report content — split into card-sized chunks
    lines = report_text.split("\n")
    chunk: list[str] = []
    for line in lines:
        chunk.append(line)
        if len(chunk) >= 15:
            elements.append({"tag": "markdown", "content": "\n".join(chunk)})
            chunk = []
    if chunk:
        elements.append({"tag": "markdown", "content": "\n".join(chunk)})

    # Footer
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "🤖 由 Claude Code 自动生成 ｜ AlphaReport 周报"}]
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📊 AlphaReport 周报 · {week_start} ~ {week_end}"},
            "template": "wathet",
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
            return False
    except Exception as e:
        logger.error(f"Card send error: {e}")
        return False


# ═══════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════


def run_weekly_report(options: WeeklyReportOptions) -> None:
    # ── Date range ──
    week_start, week_end, start_label, end_label = get_previous_week_range(options.weeks_ago)

    # ── Receive IDs ──
    receive_ids = FEISHU_RECEIVE_IDS
    if options.receive_ids:
        receive_ids = [i.strip() for i in options.receive_ids.split(",") if i.strip()]
    receive_ids = [i for i in receive_ids if i]

    receive_type = options.receive_type or FEISHU_RECEIVE_TYPE

    if not options.dry_run and not receive_ids:
        logger.error("No receive IDs. Set FEISHU_WEEKLY_PUSH_IDS or FEISHU_DAILY_PUSH_IDS in .env")
        raise ValueError("Invalid workflow options or missing destination")

    # ── Query papers ──
    logger.info("=" * 50)
    logger.info(f"Weekly range: {start_label} (Mon) ~ {end_label} (Sun)")
    logger.info("Querying by published_date (NOT created_at)...")
    papers = get_weekly_papers(week_start, week_end)

    if not papers:
        logger.warning(f"No papers published between {start_label} and {end_label}. Skipping push (silent).")
        if options.dry_run:
            print(f"\n[Dry run] No papers in range, push skipped (silent)")
        return
    else:
        # ── Generate report ──
        logger.info("=" * 50)
        if options.no_llm:
            logger.info("Using fallback template (--no-llm)")
            report_text = fallback_weekly_report(papers, start_label, end_label)
        else:
            logger.info("Generating weekly report via LLM...")
            report_text = generate_weekly_report(papers, start_label, end_label)

    if options.output:
        Path(options.output).write_text(report_text, encoding="utf-8")
        logger.info(f"Report saved to {options.output}")

    # ── Push ──
    if options.dry_run:
        logger.info("=" * 50)
        logger.info(f"[DRY RUN] Would push to: {receive_ids}")
        print("\n" + "=" * 60)
        print(report_text)
        print("=" * 60)
        print(f"\n[DRY RUN] {len(papers)} papers, push to {len(receive_ids)} recipients")
        return

    logger.info("=" * 50)
    logger.info(f"Pushing to {len(receive_ids)} recipients")
    for rid in receive_ids:
        success = send_feishu_card(rid, report_text, papers, start_label, end_label, receive_type)
        if success:
            logger.info(f"  ✓ {rid}")
        else:
            logger.error(f"  ✗ {rid}")

    logger.info("Weekly report complete!")
