"""Render saved evidence assessments without asking a model to re-rank or invent text."""
from __future__ import annotations
import json

from reportagent.skills.registry import skill_resource
from reportagent.services.paper_scoring import load_enabled_directions, relevance_reason_text
from reportagent.services.report_quality import pending_review, source_category, current_relevance


def _links(paper: dict) -> str:
    return " · ".join(f"[{label}]({paper[key]})" for label, key in (
        ("原文", "source_url"), ("PDF", "pdf_url"), ("网页详情", "ui_url")) if paper.get(key))


def _needs_verification(evaluation: dict) -> bool:
    return (evaluation.get("tier") == "pending" or evaluation.get("status") != "reviewed"
            or bool(evaluation.get("validation_issues")) or evaluation.get("text_complete") is False
            or evaluation.get("quality") == "unknown")


def _verification_reason(evaluation: dict) -> str:
    reasons = evaluation.get("validation_issues") or evaluation.get("limitations") or ["待完成全文评估"]
    readable = []
    for reason in reasons:
        if reason.startswith("引文未能定位："):
            reason = "旧版引文匹配筛查已停用，请重新生成全文阅读评估"
        if reason not in readable:
            readable.append(reason)
    return "；".join(readable[:3])


def evaluation_sections(evaluation: dict) -> dict[str, str]:
    """Use one saved assessment for single-paper messages and daily featured entries."""
    pending = _needs_verification(evaluation)
    findings = "；".join(item["claim"] for item in evaluation.get("findings", [])[:2])
    strengths = "；".join(item["claim"] for item in evaluation.get("strengths", [])[:2])
    return {
        "label": "待核验" if pending else evaluation["label"],
        "research_question": evaluation.get("research_question") or "待补充",
        "method_findings": "待核验，暂不引用方法或研究发现。" if pending else
            f"{(evaluation.get('method') or {}).get('claim', '')} {findings}".strip(),
        "strengths": "待完成全文阅读评估。" if pending else strengths or "未记录独立优势。",
        "why_read": "待完成全文阅读评估。" if pending else
            f"{relevance_reason_text(evaluation.get('relevance_reason'))} {strengths}".strip(),
        "limitations": "；".join(evaluation.get("limitations", [])[:3]) or "未记录，需进一步核查。",
        "critical_concerns": "；".join(evaluation.get("critical_concerns", [])) or "未记录明确的关键疑虑。",
        "reading_advice": evaluation.get("reading_advice") or "核对原文后再判断。",
        "verification": _verification_reason(evaluation) if pending else
            "模型已完成全文阅读评估；引文供查阅，未独立验证研究结论或复现实验。",
    }


def render_single_brief(paper: dict, sections: dict[str, str], model: str) -> str:
    return skill_resource("analyze-report", "single-brief.md").read_text(encoding="utf-8").format(
        title=paper["title"], authors=", ".join(paper.get("authors", [])),
        links=_links(paper), model=model, **sections,
    )


def render_briefing(papers: list[dict], date_label: str, *, source_statuses: list[dict] | None = None,
                    max_featured: int = 5, template_skill: str = "daily-report",
                    source_checked_at: str | None = None) -> str:
    ordered = sorted(papers, key=lambda p: (
        {"focus": 0, "browse": 1, "pending": 2, "exclude": 3}.get((p.get("evaluation") or {}).get("tier"), 2),
        -(p.get("selection_relevance") or p.get("selection_score") or 0)))
    focus = [p for p in ordered if (p.get("evaluation") or {}).get("tier") == "focus"
             and not _needs_verification(p["evaluation"])][:max_featured]
    focus_ids = {p["id"] for p in focus}
    details = []
    for i, p in enumerate(focus, 1):
        e = p["evaluation"]
        sections = evaluation_sections(e)
        category = source_category(p.get("source", ""))
        source_label = {"academic": "学术论文", "broker": "券商研报",
                        "community": "社区研究", "other": "其他材料"}[category]
        details.append(f"### {i}. {p['title']}\n"
                       f"**材料类型**：{source_label}\n\n"
                       f"**研究问题**：{sections['research_question']}\n\n"
                       f"**方法与发现**：{sections['method_findings']}\n\n"
                       f"**为什么值得读**：{sections['why_read']}\n\n"
                       f"**局限**：{sections['limitations']}\n\n"
                       f"**阅读建议**：{sections['reading_advice']}\n\n{_links(p)}")
    remaining = {"其他论文": [], "券商研报": [], "社区与其他材料": [], "待核验": [],
                 "待补全文或摘要": [], "方向相关性较低": []}
    directions = load_enabled_directions()
    excluded = 0
    for p in ordered:
        if p["id"] in focus_ids:
            continue
        e = p.get("evaluation") or pending_review(p, "尚未按新标准评估")
        pending = _needs_verification(e)
        if e["tier"] == "exclude" and not pending:
            excluded += 1
            continue
        category = source_category(p.get("source", ""))
        group = "待核验" if pending else "其他论文" if category == "academic" else "券商研报" if category == "broker" else "社区与其他材料"
        summary = _verification_reason(e) if pending else "；".join(x["claim"] for x in e.get("findings", [])[:1])
        if pending and e.get("research_question"):
            summary = f"研究问题（模型提取，待核验）：{e['research_question']}\n  核验状态：{summary}"
        if not summary:
            summary = "；".join(e.get("limitations", [])[:1]) or "待完成全文评估"
        direction_id = p.get("selection_direction")
        direction = directions.get(direction_id, {}).get("name") or direction_id or "未分方向"
        label = "待核验" if pending else e["label"]
        try:
            screening = json.loads(p.get("selection_provenance_json") or "{}")
        except (TypeError, ValueError):
            screening = {}
        if isinstance(screening, dict) and screening.get("status") == "pending_needs_material":
            group, label = "待补全文或摘要", "材料不足，尚未判断相关性"
            summary = relevance_reason_text(p.get("selection_reason")) or "补全有效摘要或正文后重评。"
        elif pending and current_relevance(p) is not None and current_relevance(p) < 6:
            group, label = "方向相关性较低", "相关性较低；研究质量未评"
            summary = relevance_reason_text(p.get("selection_reason")) or "仅依据摘要判断方向相关性，不代表论文质量低。"
        remaining[group].append(f"- **{p['title']}** · {label} · {direction}\n  {summary}\n  {_links(p)}")
    others = "\n\n".join(f"## {label}\n" + "\n".join(items) for label, items in remaining.items() if items)
    if excluded:
        others += f"\n\n另有 {excluded} 篇因明确的研究局限未推荐；评估理由保存在论文库。"
    labels = {"ok": "正常", "empty": "无新内容", "partial": "部分成功", "error": "失败", "unavailable": "不可用"}
    coverage = "\n".join(f"- {s['source']}：{labels.get(s['status'], s['status'])}，有效 {s.get('valid', 0)} 篇"
                         + (f"；{'；'.join(s.get('errors', [])[:2])}" if s.get('errors') else "") for s in (source_statuses or []))
    overview = (f"本期检查 {len(papers)} 篇库中候选，重点推荐 {len(focus)} 篇。"
                "按阅读价值与相关性优先选读，局限随文列出。")
    if source_statuses:
        valid = sum(s.get('valid', 0) for s in source_statuses)
        label = f"最近定时采集（{source_checked_at}）" if source_checked_at else "本次在线采集"
        overview = (f"{label}有效资料 {valid} 条（按来源合计）；"
                    "本期候选可包含此前已收录、符合本期日期范围的库中论文。\n\n" + overview)
        if source_checked_at:
            coverage = f"以下为最近一次定时采集记录（{source_checked_at}）；本次报告仅读取论文库。\n\n" + coverage
        if any(s['status'] in {'error', 'partial', 'unavailable'} for s in source_statuses):
            overview += " 部分来源采集失败或不完整，最新资料覆盖可能不完整。"
    return skill_resource(template_skill, "briefing.md").read_text(encoding="utf-8").format(
        date_label=date_label,
        overview=overview,
        focus="\n\n".join(details) or "暂无达到重点推荐标准的论文。",
        other_papers=others,
        source_status=coverage or "本次使用库中已有论文，未重新检查在线来源。",
    )
