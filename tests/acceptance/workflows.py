"""Explicit live acceptance scenarios. Never collected by pytest; never sends messages."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch

from reportagent.db.engine import init_db, get_session_factory
from reportagent.models.database import Report
from reportagent.utils.config import get_db_path, get_llm_model


def save(folder: Path, name: str, data):
    (folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def library_snapshot(folder: Path, label: str) -> list[dict]:
    fields = ("id", "title", "source", "source_url", "arxiv_id", "published_date", "topics",
              "abstract", "has_full_text", "pdf_path", "classification_method",
              "classification_confidence", "selection_relevance", "selection_direction",
              "selection_reason", "selection_provenance_json", "is_selected", "analysis_json",
              "evaluation_json", "knowledge_card_json")
    with get_session_factory()() as session:
        records = []
        for row in session.query(Report).order_by(Report.id).all():
            record = {key: getattr(row, key) for key in fields}
            record["full_text_chars"] = len(row.full_text or "")
            records.append(record)
            if row.full_text:
                (folder / f"paper-{row.id}.txt").write_text(row.full_text, encoding="utf-8")
    save(folder, f"{label}-library.json", records)
    return records


async def collect(args):
    from reportagent.services.collection import CollectionOptions, collect_reports
    options = CollectionOptions(topics=args.topics, sources=args.sources, max_results=args.max_per_source,
                                days=args.days, date_from=args.date_from, date_to=args.date_to,
                                no_analysis=True)
    return {"options": asdict(options), "result": await collect_reports(options)}


async def score(args):
    from reportagent.llm.client import LLMClient
    from reportagent.services.paper_scoring import score_papers_batch, apply_scores
    from reportagent.services.report_quality import review_report_ids
    from reportagent.services.report_briefing import render_briefing
    with get_session_factory()() as session:
        # No date/page cap: this acceptance scenario covers the complete library.
        papers = session.query(Report).order_by(Report.id).all()
        client = LLMClient()
        try:
            results, failures = await score_papers_batch(client, papers, batch_size=5)
            stats = apply_scores(results)
            scores = [{k: v for k, v in r.items() if k != "_paper"} | {"id": r["_paper"].id}
                      for r in results]
            calls = client.calls.copy()
        finally:
            await client._client.close()
        report_ids = [p.id for p in papers]
    save(args.run_dir, f"{args.label}-scores.json", scores)
    review_ids = [r["id"] for r in scores if r["relevance"] >= 6]
    evaluations = await review_report_ids(review_ids) if args.review else {}
    records = library_snapshot(args.run_dir, args.label)
    for r in records:
        r["evaluation"] = json.loads(r.get("evaluation_json") or "null")
        r["ui_url"] = f"http://127.0.0.1:8000/library/{r['id']}"
    if args.review:
        (args.run_dir / f"{args.label}-brief.md").write_text(
            render_briefing(records, "全库验收（不是当天日报）"), encoding="utf-8")
    return {"library_ids": report_ids, "stats": stats, "failed_batches": failures, "model_calls": calls,
            "review_requested": args.review, "review_ids": review_ids if args.review else [],
            "review_tiers": Counter(e.get("tier") for e in evaluations.values()),
            "all_scored": len(results) == len(report_ids) and not failures and stats["errors"] == 0}


async def batch(args):
    from reportagent.services.batch_analysis import AnalysisOptions, run_analysis
    if not args.topic:
        raise ValueError("batch requires --topic, selected after inspecting actual classification")
    options = AnalysisOptions(topic=args.topic, depth=args.depth)
    return {"options": asdict(options), "result": await run_analysis(options)}


async def classify(args):
    from reportagent.services.batch_classification import ClassificationOptions, classify_async
    await classify_async(ClassificationOptions(all=True, llm=True))
    records = library_snapshot(args.run_dir, args.label)
    failed = [r["id"] for r in records if not r["topics"] or not r["classification_confidence"]]
    return {"library_count": len(records), "missing_classification_ids": failed,
            "topics": Counter(t for r in records for t in (r["topics"] or "").split(",") if t)}


def weekly(args):
    from reportagent.services.weekly_report import (
        get_previous_week_range, get_weekly_papers, run_weekly_report,
        WeeklyReportOptions, fallback_weekly_report,
    )
    start, end, first, last = get_previous_week_range()
    papers = get_weekly_papers(start, end)
    save(args.run_dir, f"{args.label}-input.json", papers)
    preview = args.run_dir / f"{args.label}.md"
    options = WeeklyReportOptions(dry_run=True, output=str(preview), no_llm=not args.llm_weekly)
    # The production weekly entry does not map DeepSeek credentials. This explicit
    # acceptance setup reuses the bot mapping, and records that adjustment below.
    env = {}
    if args.llm_weekly:
        from reportagent.bot.claude import claude_environment
        if not os.environ.get("DEEPSEEK_API_KEY") or not get_llm_model().startswith("deepseek"):
            raise ValueError("Live weekly requires the configured DeepSeek account/model")
        env = claude_environment()
        env.update(ANTHROPIC_MODEL=get_llm_model(),
                   ANTHROPIC_DEFAULT_OPUS_MODEL=get_llm_model(),
                   ANTHROPIC_DEFAULT_SONNET_MODEL=get_llm_model(),
                   ANTHROPIC_DEFAULT_HAIKU_MODEL=get_llm_model())
    with patch.dict(os.environ, env), redirect_stdout(io.StringIO()):
        run_weekly_report(options)
    text = preview.read_text(encoding="utf-8") if preview.exists() else ""
    return {"start": start, "end": end, "paper_ids": [p["id"] for p in papers],
            "sources": Counter(p["source"] for p in papers), "collects_sources": False,
            "llm_requested": args.llm_weekly, "explicit_deepseek_environment": bool(env),
            "fallback": bool(text) and text == fallback_weekly_report(papers, first, last),
            "empty": not papers, "preview_chars": len(text), "sent": False}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["collect", "classify", "score", "batch", "weekly", "snapshot"])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--label", default=None)
    parser.add_argument("--topics", default="组合优化,因子模型")
    parser.add_argument("--sources", default="arxiv,eastmoney,bigquant")
    parser.add_argument("--max-per-source", type=int, default=4)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--topic")
    parser.add_argument("--depth", choices=["quick", "standard", "deep"], default="standard")
    parser.add_argument("--review", action="store_true", help="Review every relevant library paper against full text")
    parser.add_argument("--llm-weekly", action="store_true", help="Generate weekly preview with own DeepSeek via Claude Code")
    args = parser.parse_args()
    args.label = args.label or args.stage
    if Path(args.label).name != args.label:
        parser.error("label must be a filename component")
    args.run_dir = args.run_dir.resolve()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    backup = args.run_dir / "before.sqlite"
    if not backup.exists():
        with sqlite3.connect(get_db_path()) as source, sqlite3.connect(backup) as target:
            source.backup(target)
    started = datetime.now(timezone.utc).isoformat()
    # Archive an interrupted/failed run's evidence too; no automatic quality pass.
    try:
        result = (weekly(args) if args.stage == "weekly" else {} if args.stage == "snapshot"
                  else await {"collect": collect, "classify": classify, "score": score, "batch": batch}[args.stage](args))
        save(args.run_dir, f"{args.label}-result.json", {
            "started": started, "completed": datetime.now(timezone.utc).isoformat(),
            "stage": args.stage, "model": get_llm_model(), "result": result,
            "content_quality": "requires_source_review", "sent": False,
        })
    except Exception as error:
        save(args.run_dir, f"{args.label}-result.json", {
            "started": started, "stage": args.stage, "error_type": type(error).__name__,
            "status": "failed", "content_quality": "not_accepted", "sent": False,
        })
        raise
    finally:
        records = library_snapshot(args.run_dir, args.label)
        print(json.dumps({"stage": args.stage, "library_count": len(records),
                          "sources": Counter(r["source"] for r in records),
                          "output": str(args.run_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    asyncio.run(main())
