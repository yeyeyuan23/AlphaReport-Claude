"""A compact weekly reading guide built only from saved, current assessments."""

from collections import Counter, defaultdict

from reportagent.services.paper_scoring import load_enabled_directions
from reportagent.services.public_url import get_library_url
from reportagent.services.report_briefing import (
    _links,
    _needs_verification,
    _verification_reason,
)
from reportagent.services.report_periods import publication_time, REPORT_TIMEZONE
from reportagent.services.report_quality import pending_review
from reportagent.skills.registry import skill_resource


def _excerpt(value, limit=160):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _day(paper):
    stamp = publication_time(paper)
    return (
        stamp.astimezone(REPORT_TIMEZONE).strftime("%Y-%m-%d")
        if stamp
        else "日期待确认"
    )


def _evaluation(paper):
    return paper.get("evaluation") or pending_review(paper, "后台全文评估尚未完成。")


def _finding(paper):
    findings = _evaluation(paper).get("findings") or []
    return _excerpt(findings[0].get("claim")) if findings else "未记录研究发现。"


def _updates(papers):
    directions = load_enabled_directions()
    grouped = defaultdict(list)
    for paper in papers:
        grouped[paper.get("selection_direction") or "other"].append(paper)
    lines = []
    for direction, rows in sorted(
        grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])
    ):
        name = directions.get(direction, {}).get("name") or direction
        findings, seen = [], set()
        for paper in rows:
            claim = _finding(paper)
            if claim in seen:
                continue
            seen.add(claim)
            url = paper.get("ui_url") or paper.get("source_url")
            evidence = f" [依据]({url})" if url else ""
            findings.append(f"{claim}{evidence}")
            if len(findings) == 2:
                break
        lines.append(f"**{name} · {len(rows)} 篇**\n" + "\n\n".join(findings))
    return "\n\n".join(lines) or "本周尚无已完成全文评估的研究更新可归纳。"


def _recommended_entry(paper):
    evaluation = _evaluation(paper)
    quality = {"strong": "质量较强", "sound": "基本扎实"}.get(
        evaluation.get("quality"), ""
    )
    strengths = evaluation.get("strengths") or evaluation.get("findings") or []
    reason = (
        _excerpt(strengths[0].get("claim"), 120)
        if strengths
        else "查看全文评估中的研究问题与结论。"
    )
    limitations = evaluation.get("limitations") or []
    note = f"；提醒：{_excerpt(limitations[0], 80)}" if limitations else ""
    return f"- **{paper['title']}** · {quality}\n  选读理由摘录：{reason.rstrip('。；;')}{note}\n  {_links(paper)}"


def _recommendations(papers):
    grouped = defaultdict(list)
    for paper in papers:
        grouped[_day(paper)].append(paper)
    return (
        "\n\n".join(
            f"### {day} · {len(rows)} 篇\n\n"
            + "\n\n".join(_recommended_entry(p) for p in rows)
            for day, rows in sorted(grouped.items(), reverse=True)
        )
        or "暂无达到重点推荐标准的论文。"
    )


def _other_entries(papers):
    browse, pending, excluded = [], [], 0
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
        else:
            browse.append(
                f"- **{paper['title']}** · {_day(paper)}\n  {_finding(paper)}\n  {_links(paper)}"
            )
    sections = []
    for heading, rows in (("其他可浏览论文", browse), ("待核验", pending)):
        if rows:
            sections.append(f"## {heading}\n\n" + "\n\n".join(rows))
    if excluded:
        sections.append(f"另有 {excluded} 篇因明确的研究局限未推荐；理由保存在论文库。")
    return "\n\n".join(sections)


def render_weekly_briefing(papers, date_label, *, publication_window=""):
    ordered = sorted(
        papers, key=lambda p: (-(p.get("selection_relevance") or 0), p["id"])
    )
    reviewed = [p for p in ordered if not _needs_verification(_evaluation(p))]
    focus = [p for p in reviewed if _evaluation(p).get("tier") == "focus"]
    focus_ids = {p["id"] for p in focus}
    overview = f"本周收录 {len(papers)} 篇，已完成全文评估 {len(reviewed)} 篇，重点推荐 {len(focus)} 篇。"
    if publication_window:
        overview = f"公开发布时间范围：{publication_window}\n\n{overview}"
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
            research_updates=_updates(
                [p for p in reviewed if _evaluation(p).get("tier") != "exclude"]
            ),
            focus=_recommendations(focus),
            other_papers=_other_entries(
                [p for p in ordered if p["id"] not in focus_ids]
            ),
            source_status=f"本期库中资料：{coverage}。仅读取已有资料与评估，未重新检查在线来源。",
            library_url=get_library_url(),
        )
    )
