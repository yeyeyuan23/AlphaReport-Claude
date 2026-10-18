"""A compact weekly reading guide built only from saved, current assessments."""

from collections import Counter, defaultdict

from reportagent.services.paper_scoring import load_enabled_directions
from reportagent.services.public_url import get_library_url
from reportagent.services.report_briefing import (
    _links,
    _needs_verification,
    _verification_reason,
)
from reportagent.services.report_quality import pending_review
from reportagent.skills.registry import skill_resource


def _excerpt(value, limit=160):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _evaluation(paper):
    return paper.get("evaluation") or pending_review(paper, "后台全文评估尚未完成。")


def research_papers(papers):
    return [
        p
        for p in papers
        if not _needs_verification(_evaluation(p))
        and _evaluation(p).get("tier") != "exclude"
    ]


def _updates(papers, summaries=None):
    directions = load_enabled_directions()
    grouped = defaultdict(list)
    for paper in papers:
        grouped[paper.get("selection_direction") or "other"].append(paper)
    lines = []
    for direction, rows in sorted(
        grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])
    ):
        name = directions.get(direction, {}).get("name") or direction
        by_id = {p["id"]: p for p in rows}
        updates = (summaries or {}).get(direction)
        if updates is None:
            updates = [
                {
                    "text": (_evaluation(p).get("findings") or [{}])[0].get("claim")
                    or "未记录研究发现。",
                    "paper_ids": [p["id"]],
                }
                for p in rows
            ]
        paragraph = " ".join(" ".join(update["text"].split()) for update in updates)
        paper_ids = dict.fromkeys(
            rid for update in updates for rid in update["paper_ids"]
        )
        links = []
        for rid in paper_ids:
            paper = by_id[rid]
            url = paper.get("ui_url") or paper.get("source_url")
            links.append(f"[依据：{paper['title']}]({url})" if url else paper["title"])
        lines.append(f"**{name} · {len(rows)} 篇**\n{paragraph}\n" + " · ".join(links))
    return "\n\n".join(lines) or "本周尚无已完成全文评估的研究更新可归纳。"


def _pending_entries(papers):
    pending, excluded = [], 0
    for paper in papers:
        evaluation = _evaluation(paper)
        if _needs_verification(evaluation):
            question = _excerpt(evaluation.get("research_question"), 100)
            note = f"研究问题（待核验）：{question}；" if question else ""
            pending.append(
                f"- **{paper['title']}** · 待核验\n  {note}{_excerpt(_verification_reason(evaluation))}\n  {_links(paper)}"
            )
        elif evaluation.get("tier") == "exclude":
            excluded += 1
    sections = []
    if pending:
        sections.append("## 待核验\n\n" + "\n\n".join(pending))
    if excluded:
        sections.append(f"另有 {excluded} 篇因明确的研究局限未推荐；理由保存在论文库。")
    return "\n\n".join(sections)


def render_weekly_briefing(
    papers, date_label, *, publication_window="", summaries=None, summary_notice=""
):
    ordered = sorted(
        papers, key=lambda p: (-(p.get("selection_relevance") or 0), p["id"])
    )
    reviewed = [p for p in ordered if not _needs_verification(_evaluation(p))]
    focus = [p for p in reviewed if _evaluation(p).get("tier") == "focus"]
    overview = f"本周收录 {len(papers)} 篇，已完成全文评估 {len(reviewed)} 篇，重点推荐 {len(focus)} 篇。"
    if publication_window:
        overview = f"来源日期范围：{publication_window}\n\n{overview}"
    sources = Counter(p.get("source") or "未知来源" for p in papers)
    coverage = (
        "、".join(f"{source} {count} 篇" for source, count in sorted(sources.items()))
        or "本期暂无资料"
    )
    return (
        skill_resource("weekly-report", "briefing.md")
        .read_text(encoding="utf-8")
        .format(
            date_label=date_label,
            overview=overview,
            research_updates=(summary_notice + "\n\n" if summary_notice else "")
            + _updates(research_papers(ordered), summaries),
            pending_papers=_pending_entries(ordered),
            source_status=f"本期库中资料：{coverage}。仅读取已有资料与评估，未重新检查在线来源。",
            library_url=get_library_url(),
        )
    )
