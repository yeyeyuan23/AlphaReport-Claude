"""Scoped literature-backed hypotheses with explicit retrieval and novelty limits."""

from __future__ import annotations

import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from reportagent.db.engine import get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.services.knowledge_cards import current_review, report_snapshot
from reportagent.skills.registry import skill_resource

ANGLES = {"general", "methodology", "factor", "cross_pollination", "critique", "data"}


class Idea(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    hypothesis: str = Field(min_length=1)
    gap_status: Literal["candidate"]
    closest_paper_ids: list[StrictInt]
    difference: str = Field(min_length=1)
    data_requirements: str = Field(min_length=1)
    minimal_experiment: str = Field(min_length=1)
    falsification: str = Field(min_length=1)


class Ideas(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ideas: list[Idea] = Field(min_length=1, max_length=3)


def collect_context(topic: str, *, verify_external=False) -> dict:
    context = {
        "retrieval_status": "empty",
        "papers": [],
        "external_status": "not_requested",
        "external_leads": [],
    }
    try:
        from reportagent.vector_store.store import get_vector_store

        matches = get_vector_store().search(query=topic, limit=10, min_quant_score=0.02)
        seen = set()
        with get_session_factory()() as session:
            repo = ReportRepository(session)
            for match in matches:
                rid = match["report_id"]
                if rid in seen:
                    continue
                seen.add(rid)
                report = repo.get_report(rid)
                if report is None:
                    continue
                review = current_review(report_snapshot(report))
                p = {
                    "id": rid,
                    "title": report.title,
                    "source_url": report.source_url or "",
                    "abstract": report.abstract or "",
                    "evidence_scope": "abstract",
                }
                if review:
                    p.update(
                        evidence_scope="saved_full_text_review",
                        method=review["method"],
                        findings=review["findings"],
                        limitations=review["limitations"],
                    )
                context["papers"].append(p)
                if len(context["papers"]) == 8:
                    break
        context["retrieval_status"] = "available" if context["papers"] else "empty"
    except Exception:
        context["retrieval_status"] = "error"
        context["papers"] = []
    if verify_external:
        import os

        if not os.environ.get("TAVILY_API_KEY"):
            context["external_status"] = "unavailable"
        else:
            try:
                from tavily import TavilyClient

                result = TavilyClient(api_key=os.environ["TAVILY_API_KEY"]).search(
                    query=topic + " research paper", max_results=5
                )
                context["external_leads"] = [
                    {
                        "title": x.get("title", ""),
                        "url": x.get("url", ""),
                        "snippet": x.get("content", ""),
                    }
                    for x in result.get("results", [])
                ]
                context["external_status"] = "leads_only"
            except Exception:
                context["external_status"] = "error"
    return context


async def generate_ideas(
    topic: str, angle: str = "general", *, verify_external=False, llm=None
) -> str:
    if not topic.strip() or angle not in ANGLES:
        raise ValueError("需要研究主题和有效的思考角度")
    import asyncio

    context = await asyncio.to_thread(
        collect_context, topic, verify_external=verify_external
    )
    if context["retrieval_status"] == "error":
        return "研报库检索失败；请检查索引与数据连接后重试。尚未完成文献检索，不能判断研究空白。"
    serialized = json.dumps(context, ensure_ascii=False)
    if len(serialized) > 60000:
        return "检索材料超过本轮完整输入上限；请缩小主题后重试。未截断材料或生成新颖性结论。"
    scope = (
        "库内有限检索：仅覆盖返回的相关论文，不代表完整领域查新。"
        if context["papers"]
        else "库内未找到相关材料，不代表领域内没有相关研究；以下仅为未查新的探索假设。"
    )
    if context["external_status"] == "leads_only":
        scope += " 已检索外部网页线索，尚未核对这些论文全文，不能确认新颖性。"
    elif context["external_status"] in ("unavailable", "error"):
        scope += " 外部检索不可用或失败，未完成外部查新。"
    else:
        scope += " 本轮未执行外部查新。"
    try:
        if llm is None:
            from reportagent.llm.client import LLMClient

            llm = LLMClient()
        result = await llm.chat_json(
            [
                {
                    "role": "system",
                    "content": skill_resource(
                        "research-ideation", "system.md"
                    ).read_text(encoding="utf-8"),
                },
                {
                    "role": "user",
                    "content": skill_resource("research-ideation", "user.md")
                    .read_text(encoding="utf-8")
                    .format(topic=topic, angle=angle, context=serialized),
                },
            ],
            temperature=0.3,
            max_tokens=3500,
        )
        ideas = Ideas.model_validate(result).ideas
        ids = {p["id"] for p in context["papers"]}
        for idea in ideas:
            if not set(idea.closest_paper_ids).issubset(ids):
                raise ValueError("模型引用了未提供的文献")
            if ids and not idea.closest_paper_ids:
                raise ValueError("已有材料时必须指出最接近的文献")
    except Exception:
        return scope + "\n\n创意结果不完整或引用无效，待重试；尚未发布研究空白判断。"
    lines = [scope]
    for n, idea in enumerate(ideas, 1):
        lines += [
            f"\n### 候选假设 {n}",
            idea.hypothesis,
            "最接近的库内工作："
            + (
                ", ".join(f"[{rid}]" for rid in idea.closest_paper_ids)
                or "尚未找到，待查新"
            ),
            "候选差异（未确认创新）：" + idea.difference,
            "数据需求：" + idea.data_requirements,
            "最小验证：" + idea.minimal_experiment,
            "否定条件：" + idea.falsification,
        ]
    lines += ["\n### 本轮参考材料"]
    for p in context["papers"]:
        lines.append(
            f"[{p['id']}] {p['title']} {p['source_url']}（{'已保存全文评估' if p['evidence_scope'] == 'saved_full_text_review' else '仅摘要'}）"
        )
    for lead in context["external_leads"]:
        lines.append(f"外部待核对线索：{lead['title']} {lead['url']}")
    return "\n".join(lines)
