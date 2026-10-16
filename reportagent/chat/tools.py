from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Optional

from langchain_core.tools import tool

from reportagent.db.engine import get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.models.schemas import (
    UserCriteria,
    SourceType,
    ReportListParams,
)
from reportagent.utils.config import PROJECT_ROOT

logger = logging.getLogger(__name__)


def _get_repo() -> tuple:
    factory = get_session_factory()
    session = factory()
    return ReportRepository(session), session


# ── 1. collect_reports ──────────────────────────────────────────

@tool
async def collect_reports(
    topics: list[str],
    sources: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
    max_results_per_source: int = 20,
) -> str:
    """按主题和数据源收集量化金融研报。触发完整的收集→分类→存储流程。

    Args:
        topics: 搜索主题列表，如 ["量化策略", "因子模型", "高频交易"]
        sources: 数据源列表，可选 "arxiv", "eastmoney", "bigquant", "local_pdf"。默认全部启用。
        keywords: 额外关键词
        max_results_per_source: 每个数据源最大结果数，默认20
    """
    from reportagent.agents.graph import build_collection_graph

    valid_sources = {s.value for s in SourceType}
    source_types = []
    if sources:
        for s in sources:
            if s in valid_sources:
                source_types.append(SourceType(s))
    if not source_types:
        source_types = [SourceType.ARXIV, SourceType.EASTMONEY, SourceType.BIGQUANT]

    criteria = UserCriteria(
        topics=topics,
        sources=source_types,
        keywords=keywords or [],
        max_results_per_source=max_results_per_source,
    )

    graph = build_collection_graph()
    initial_state = {
        "criteria": criteria,
        "task_id": str(uuid.uuid4()),
        "raw_results": [],
        "collection_status": "pending",
        "collection_errors": [],
        "classified_reports": [],
        "classification_status": "pending",
        "storage_result": None,
        "storage_status": "pending",
        "current_phase": "init",
        "messages": [],
    }

    result = await graph.ainvoke(initial_state)

    sr = result.get("storage_result")
    errors = result.get("collection_errors", [])
    msgs = result.get("messages", [])

    summary_parts = []
    if sr:
        summary_parts.append(
            f"收集完成: 处理 {sr.total_processed} 篇，"
            f"新增 {sr.newly_added} 篇，更新 {sr.updated} 篇，"
            f"跳过 {sr.duplicate_skipped} 篇重复"
        )
    if errors:
        summary_parts.append(f"错误: {'; '.join(errors[:3])}")
    if msgs:
        summary_parts.append(f"日志: {msgs[-1]}")

    return "\n".join(summary_parts) if summary_parts else "收集完成，无新增研报。"


# ── 2. search_reports ───────────────────────────────────────────

@tool
def search_reports(
    query: Optional[str] = None,
    topic: Optional[str] = None,
    source: Optional[str] = None,
    market: Optional[str] = None,
    limit: int = 10,
) -> str:
    """搜索研报库中的研报。可按关键词、主题、来源等筛选。

    Args:
        query: 搜索关键词（匹配标题和摘要）
        topic: 主题筛选，如 "factor_model", "risk_model", "ai_ml_model"
        source: 数据源筛选，如 "arxiv", "eastmoney", "bigquant"
        market: 市场筛选，如 "china", "overseas", "global"
        limit: 返回数量上限，默认10
    """
    repo, session = _get_repo()
    try:
        params = ReportListParams(
            search=query,
            topic=topic,
            source=source,
            market=market,
            limit=min(limit, 20),
        )
        reports, total = repo.list_reports(params)

        if not reports:
            return f"未找到匹配的研报。(搜索条件: query={query}, topic={topic}, source={source})"

        lines = [f"找到 {total} 篇研报 (显示前 {len(reports)} 篇):\n"]
        for r in reports:
            topics_str = r.topics or ""
            date = r.published_date.strftime("%Y-%m-%d") if r.published_date else "未知"
            lines.append(f"  [{r.id}] {r.title}")
            lines.append(f"      来源: {r.source} | 日期: {date} | 主题: {topics_str}")
            if r.abstract:
                lines.append(f"      摘要: {r.abstract[:100]}...")
            lines.append("")
        return "\n".join(lines)
    finally:
        session.close()


# ── 3. get_report ───────────────────────────────────────────────

@tool
def get_report(report_id: int) -> str:
    """获取单篇研报的完整详情，包括摘要和全文。

    Args:
        report_id: 研报ID
    """
    repo, session = _get_repo()
    try:
        r = repo.get_report(report_id)
        if not r:
            return f"研报 ID={report_id} 不存在。"

        authors = json.loads(r.authors) if r.authors else []
        parts = [
            f"# {r.title}",
            f"ID: {r.id}",
            f"作者: {', '.join(authors) if authors else '未知'}",
            f"来源: {r.source}",
            f"日期: {r.published_date.strftime('%Y-%m-%d') if r.published_date else '未知'}",
            f"主题: {r.topics or '未分类'}",
            f"市场: {r.markets or '未分类'}",
            f"资产: {r.asset_classes or '未分类'}",
        ]
        if r.source_url:
            parts.append(f"链接: {r.source_url}")
        if r.abstract:
            parts.append(f"\n## 摘要\n{r.abstract}")
        if r.full_text:
            text = r.full_text[:3000]
            parts.append(f"\n## 全文 (前3000字)\n{text}")
        elif r.abstract:
            parts.append("\n(仅有摘要，无完整全文)")
        return "\n".join(parts)
    finally:
        session.close()


# ── 4. analyze_report ───────────────────────────────────────────

@tool
async def analyze_report(
    report_id: int,
    analysis_type: str = "summary",
) -> str:
    """用 LLM 对指定研报做深度分析。

    Args:
        report_id: 研报ID
        analysis_type: 分析类型，可选:
            "summary" — 核心内容提炼
            "methodology" — 方法论解读
            "findings" — 关键发现与结论
            "strategy" — 策略评估与可行性
            "critique" — 优缺点分析
    """
    from reportagent.llm.client import LLMClient

    repo, session = _get_repo()
    try:
        r = repo.get_report(report_id)
        if not r:
            return f"研报 ID={report_id} 不存在。"

        content = r.full_text or r.abstract or ""
        if not content:
            return f"研报「{r.title}」没有可分析的文本内容。"

        type_prompts = {
            "summary": "请提炼这篇量化金融研报的核心内容，包括研究问题、方法、数据和主要结论。",
            "methodology": "请详细解读这篇研报使用的方法论和技术路线，包括模型、算法、数据处理流程等。",
            "findings": "请总结这篇研报的关键发现和结论，以及对量化投资实践的启示。",
            "strategy": "请评估这篇研报中提出的策略的可行性，包括潜在收益、风险、实现难度和局限性。",
            "critique": "请从学术和实务角度分析这篇研报的优点和不足。",
        }
        instruction = type_prompts.get(analysis_type, type_prompts["summary"])

        prompt = (
            f"{instruction}\n\n"
            "【强制格式要求】数学符号和公式必须用 LaTeX 格式：行内用 $...$，独立公式用 $$...$$。"
            "禁止用纯文本描述数学符号。\n\n"
            f"## 研报标题\n{r.title}\n\n## 研报内容\n{content[:4000]}"
        )

        client = LLMClient()
        result = await client.chat(
            [{"role": "user", "content": prompt}], max_tokens=3000
        )

        return f"## 「{r.title}」分析 ({analysis_type})\n\n{result}"
    finally:
        session.close()


# ── 5. delete_reports ───────────────────────────────────────────

@tool
def delete_reports(report_ids: list[int]) -> str:
    """从研报库中删除指定的研报。

    Args:
        report_ids: 要删除的研报ID列表
    """
    repo, session = _get_repo()
    try:
        deleted = []
        not_found = []
        for rid in report_ids:
            if repo.delete_report(rid):
                deleted.append(rid)
            else:
                not_found.append(rid)

        parts = []
        if deleted:
            parts.append(f"已删除 {len(deleted)} 篇研报 (ID: {deleted})")
        if not_found:
            parts.append(f"未找到 {len(not_found)} 篇 (ID: {not_found})")
        return "\n".join(parts) if parts else "无操作。"
    finally:
        session.close()


# ── 6. semantic_search_reports ───────────────────────────────────

@tool
def semantic_search_reports(
    query: str,
    limit: int = 10,
    source: str | None = None,
    topic: str | None = None,
    market: str | None = None,
) -> str:
    """用语义搜索在研报库中查找相关论文。支持中英文混合查询，按语义相关性排序。

    Args:
        query: 搜索查询，可以是中文或英文。例如 "波动率择时策略" 或 "volatility timing with machine learning"
        limit: 返回数量上限，默认10
        source: 可选的数据源筛选，如 "arxiv", "eastmoney", "bigquant"
        topic: 可选的主题筛选，如 "factor_model", "ai_ml_model", "volatility"
        market: 可选的市场筛选，如 "china", "overseas", "global"
    """
    try:
        from reportagent.vector_store.store import get_vector_store

        store = get_vector_store()
        results = store.search(
            query=query,
            limit=limit,
            source=source,
            topic=topic,
            market=market,
            min_quant_score=0.02,
        )

        if not results:
            return f"未找到与「{query}」语义相关的量化金融研报。"

        lines = [f"语义搜索「{query}」找到 {len(results)} 篇量化相关研报:\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"  [{r['report_id']}] {r['title']}")
            lines.append(
                f"      相关性: {r['score']:.2f} | 量化度: {r.get('quant_score', 0):.3f} "
                f"| 来源: {r.get('source', '')} | 主题: {r.get('topics', '')}"
            )
            if r.get("match_text"):
                lines.append(f"      匹配片段: {r['match_text'][:200]}...")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"语义搜索失败: {e}。请确认向量库已初始化并有索引数据。"


# ── 7. web_search ───────────────────────────────────────────────

@tool
def web_search(query: str, max_results: int = 5) -> str:
    """使用 Tavily 搜索引擎搜索网页，获取量化金融相关的最新资讯、论文或新闻。

    Args:
        query: 搜索关键词
        max_results: 最大结果数，默认5
    """
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        return "Tavily API Key 未配置，无法进行网页搜索。"

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=api_key)
        results = client.search(query, max_results=min(max_results, 10))

        items = results.get("results", [])
        if not items:
            return f"未找到与「{query}」相关的网页结果。"

        lines = [f"搜索「{query}」找到 {len(items)} 条结果:\n"]
        for i, item in enumerate(items, 1):
            lines.append(f"  {i}. {item.get('title', '无标题')}")
            lines.append(f"     URL: {item.get('url', '')}")
            content = item.get("content", "")
            if content:
                lines.append(f"     摘要: {content[:200]}...")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"网页搜索失败: {e}"


# ── 7. manage_skill ─────────────────────────────────────────────

@tool
def manage_skill(
    action: str,
    name: str,
    description: str = "",
    prompt_template: str = "",
) -> str:
    """创建、更新或删除用户自定义 skill。

    Args:
        action: 操作类型 — "create"（创建）, "update"（更新）, "delete"（删除）
        name: skill 名称
        description: skill 描述（create/update 时需要）
        prompt_template: skill 的 prompt 模板（create/update 时需要）
    """
    from reportagent.skills.registry import save_user_skill, delete_user_skill, get_skill

    if action in ("create", "update"):
        if not description or not prompt_template:
            return "创建/更新 skill 需要 description 和 prompt_template。"
        path = save_user_skill(name, description, prompt_template)
        return f"Skill「{name}」已{'更新' if action == 'update' else '创建'}: {path}"
    elif action == "delete":
        existing = get_skill(name)
        if existing and existing.get("_source") == "builtin":
            return f"「{name}」是内置 skill，不能删除。"
        if delete_user_skill(name):
            return f"Skill「{name}」已删除。"
        return f"未找到名为「{name}」的用户 skill。"
    else:
        return f"未知操作: {action}。请使用 create / update / delete。"


# ── 8. list_skills ──────────────────────────────────────────────

@tool
def list_skills() -> str:
    """列出所有可用的 skills（内置 + 用户自定义）。"""
    from reportagent.skills.registry import load_all_skills

    skills = load_all_skills()
    if not skills:
        return "当前没有任何 skill。"

    lines = [f"共 {len(skills)} 个 skill:\n"]
    for s in skills:
        source = "内置" if s.get("_source") == "builtin" else "自定义"
        lines.append(f"  [{source}] {s['name']}: {s.get('description', '')}")
    return "\n".join(lines)


# ── 9. parse_document ──────────────────────────────────────────

@tool
def parse_document(pdf_path: str) -> str:
    """使用 MinerU 深度解析 PDF 文档，提取结构化内容（文本、表格、公式、图片）。

    Args:
        pdf_path: PDF 文件路径
    """
    from pathlib import Path

    path = Path(pdf_path)
    if not path.exists():
        return f"文件不存在: {pdf_path}"
    if not path.suffix.lower() == ".pdf":
        return f"仅支持 PDF 文件: {pdf_path}"

    try:
        from reportagent.processors.mineru_parser import MinerUParser
        parser = MinerUParser()
        result = parser.parse(path)
    except Exception as e:
        return f"解析失败: {e}"

    parts = [
        f"# 文档解析结果: {path.name}",
        f"页数: {result.page_count}",
        f"文本段落: {len(result.text.split(chr(10)))} 段",
        f"表格: {len(result.tables)} 个",
        f"公式: {len(result.equations)} 个",
        f"图片: {len(result.images)} 个",
    ]

    if result.text:
        parts.append(f"\n## 文本内容 (前3000字)\n{result.text[:3000]}")

    for i, table in enumerate(result.tables[:5]):
        caption = ", ".join(table.get("table_caption", []))
        body = table.get("table_body", "")
        label = f"表 {i+1}" + (f": {caption}" if caption else "")
        parts.append(f"\n## {label} (第{table.get('page_idx', 0)+1}页)\n{body[:1000]}")

    for i, eq in enumerate(result.equations[:10]):
        latex = eq.get("latex", "")
        if latex:
            parts.append(f"\n公式 {i+1} (第{eq.get('page_idx', 0)+1}页): ${latex}$")

    return "\n".join(parts)


# ── 10. brainstorm_research ───────────────────────────────────────

@tool
async def brainstorm_research(topic: str, angle: str = "general", verify_external: bool = False) -> str:
    """从有限文献提出候选研究假设与最小验证，不确认新颖性。

    angle: general, methodology, factor, cross_pollination, critique, data。
    verify_external: 显式请求查新时搜索外部线索；网页结果不等于核对全文。
    """
    from reportagent.services.research_ideation import generate_ideas
    return await generate_ideas(topic, angle, verify_external=verify_external)


# ── 11. recall_discussed_reports ───────────────────────────────────

@tool
def recall_discussed_reports(
    report_ids: list[int] | None = None,
    show_all: bool = True,
) -> str:
    """回顾当前对话中已讨论过的研报。当用户说"刚才那篇论文"、"之前讨论的"时可调用。

    默认显示所有已讨论的研报摘要。如果指定 report_ids，则仅显示对应研报的详细信息。

    Args:
        report_ids: 指定要回顾的研报 ID 列表，为空则显示全部
        show_all: 是否显示所有已讨论的研报，默认 True
    """
    repo, session = _get_repo()
    try:
        if report_ids:
            lines = []
            for rid in report_ids:
                r = repo.get_report(rid)
                if r:
                    lines.append(f"[{r.id}] {r.title}")
                    lines.append(f"    来源: {r.source} | 主题: {r.topics or '未分类'}")
                    if r.abstract:
                        lines.append(f"    摘要: {r.abstract[:200]}")
                    lines.append("")
                else:
                    lines.append(f"[{rid}] 未在库中找到\n")
            return "\n".join(lines) if lines else "未找到指定研报。"
        else:
            return "请通过对话上下文或指定 report_ids 来回顾已讨论的研报。"
    finally:
        session.close()


ALL_TOOLS = [
    collect_reports,
    search_reports,
    semantic_search_reports,
    get_report,
    analyze_report,
    delete_reports,
    web_search,
    manage_skill,
    list_skills,
    parse_document,
    brainstorm_research,
    recall_discussed_reports,
]
