from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from reportagent.api.deps import get_db
from reportagent.services.report_views import read_current_analysis
from reportagent.db.repository import ReportRepository
from reportagent.models.schemas import ReportListParams

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


def _report_to_summary(r) -> dict:
    custom_tags = [t.value for t in r.tags if t.dimension == "custom"] if r.tags else []

    return {
        "id": r.id,
        "title": r.title,
        "authors": json.loads(r.authors) if r.authors else [],
        "abstract": r.abstract,
        "source": r.source,
        "source_url": r.source_url,
        "doi": r.doi,
        "arxiv_id": r.arxiv_id,
        "published_date": r.published_date.isoformat() if r.published_date else None,
        "has_full_text": r.has_full_text,
        "pdf_path": r.pdf_path,
        "markets": r.markets.split(",") if r.markets else [],
        "asset_classes": r.asset_classes.split(",") if r.asset_classes else [],
        "frequencies": r.frequencies.split(",") if r.frequencies else [],
        "topics": r.topics.split(",") if r.topics else [],
        "custom_tags": custom_tags,
        "content_hash": r.content_hash,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@router.get("")
async def list_reports(
    market: Optional[str] = Query(None),
    asset_class: Optional[str] = Query(None),
    frequency: Optional[str] = Query(None),
    topic: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    has_full_text: Optional[bool] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    sort_by: str = Query("created_at"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    repo = ReportRepository(db)
    params = ReportListParams(
        market=market,
        asset_class=asset_class,
        frequency=frequency,
        topic=topic,
        search=search,
        source=source,
        has_full_text=has_full_text,
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
    )
    reports, total = repo.list_reports(params)
    return {
        "success": True,
        "data": {
            "reports": [_report_to_summary(r) for r in reports],
            "total": total,
            "limit": limit,
            "offset": offset,
        },
    }


@router.get("/stats")
async def get_stats(db: Session = Depends(get_db)):
    repo = ReportRepository(db)
    stats = repo.get_stats()
    return {"success": True, "data": stats}


@router.get("/{report_id}")
async def get_report(report_id: int, db: Session = Depends(get_db)):
    repo = ReportRepository(db)
    report = repo.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    data = _report_to_summary(report)
    data["full_text"] = report.full_text
    data["summary"] = report.summary
    data["tables_json"] = report.tables_json
    data["equations_json"] = report.equations_json
    analysis = read_current_analysis(report)
    if analysis is not None:
        data["analysis"] = analysis
    return {"success": True, "data": data}


@router.post("/{report_id}/summarize")
async def summarize_report(report_id: int, db: Session = Depends(get_db)):
    repo = ReportRepository(db)
    report = repo.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    content = report.full_text or report.abstract or ""
    if not content:
        raise HTTPException(status_code=400, detail="No content to summarize")

    from reportagent.llm.client import LLMClient
    import json as _json

    # If analysis exists, synthesize from structured results
    analysis_data = read_current_analysis(report)

    if analysis_data and analysis_data.get("methodology"):
        prompt = _build_summary_from_analysis(analysis_data, report.title)
        max_tokens = 3000
    else:
        prompt = _build_summary_from_content(report, content)
        max_tokens = 4000

    client = LLMClient()

    from reportagent.agents.analysis_agent import _normalize_latex_delimiters

    async def stream_summary():
        full_summary = []
        async for chunk in client.chat_stream(
            [{"role": "user", "content": prompt}], max_tokens=max_tokens
        ):
            chunk = _normalize_latex_delimiters(chunk)
            full_summary.append(chunk)
            yield f"data: {_json.dumps({'type': 'token', 'content': chunk}, ensure_ascii=False)}\n\n"

        summary_text = "".join(full_summary)
        repo2 = ReportRepository(db)
        repo2.update_summary(report_id, summary_text)
        yield f"data: {_json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(stream_summary(), media_type="text/event-stream")


def _build_summary_from_analysis(analysis: dict, title: str) -> str:
    """Build a prompt that synthesizes the structured analysis into a summary."""
    steps_text = ""
    methodology = analysis.get("methodology") or {}
    equations = analysis.get("equations", [])

    if methodology.get("analysis_points"):
        for point in methodology["analysis_points"]:
            if isinstance(point, dict):
                point_title = point.get("title", "")
                point_analysis = point.get("analysis", "")
                steps_text += f"\n- **{point_title}**：{point_analysis}"
                if point.get("marginal_contribution"):
                    steps_text += f"\n  边际贡献：{point['marginal_contribution']}"
                if point.get("practical_implication"):
                    steps_text += f"\n  实践推论：{point['practical_implication']}"
                for fi in point.get("related_formulas", []):
                    if fi < len(equations):
                        eq = equations[fi]
                        steps_text += f"\n  对应公式 ${eq['latex']}$"
                        if eq.get("meaning"):
                            steps_text += f"（{eq['meaning']}）"
    else:
        for eq in equations:
            if eq.get("is_key_formula") or eq.get("latex"):
                steps_text += f"\n- 公式：${eq['latex']}$"
                if eq.get("meaning"):
                    steps_text += f" — {eq['meaning']}"

    factors_text = ""
    for f in methodology.get("factor_list", []):
        if isinstance(f, dict):
            factors_text += f"\n- **{f.get('name', '')}**（{f.get('type', '')}）：{f.get('construction', '')}"

    assessment = analysis.get("assessment") or {}
    strengths = "\n".join(f"- {s}" for s in assessment.get("strengths", []))
    weaknesses = "\n".join(f"- {w}" for w in assessment.get("weaknesses", []))
    marginal_summary = assessment.get("marginal_contribution_summary", "")
    implications = "\n".join(
        f"- {imp}" for imp in assessment.get("practical_implications", [])
    )

    return (
        "你是一位量化金融研究总结专家。请根据以下结构化的论文分析结果，"
        "撰写一份精炼的研报总结。\n\n"
        "【输出要求】\n"
        "- 以自然的叙述方式撰写，像一篇学术摘要一样流畅连贯。\n"
        "- 总结应自然涵盖：研究问题与核心结论、方法步骤与关键公式、因子构建逻辑、"
        "边际贡献（相对基准的增量在哪）、实践推论（从业者该做什么不同的事）、综合评估。\n"
        "- 不要使用分节标题，用自然的段落过渡。\n\n"
        "【强制格式要求 - 必须严格遵守】\n"
        "- 所有文本字段用中文输出。\n"
        "- 数学符号、变量名用 $...$ LaTeX 格式。\n"
        "- 完整公式用 $$...$$ 单独成行。\n"
        "- 严禁使用 \\(...\\) 或 \\[...\\] 格式。\n\n"
        f"论文标题：{title}\n\n"
        f"研究问题：{analysis.get('research_question', '')}\n"
        f"核心贡献：{analysis.get('core_contribution', '')}\n\n"
        f"方法步骤及对应公式：{steps_text}\n\n"
        f"因子信息：{factors_text}\n\n"
        f"优势：\n{strengths}\n\n"
        f"不足：\n{weaknesses}\n\n"
        f"边际贡献总评：{marginal_summary}\n\n"
        f"实践推论：\n{implications}\n\n"
        f"综合评分：{assessment.get('overall_quality_score', '')}"
    )


def _strip_boilerplate(text: str) -> str:
    """Strip Chinese brokerage report boilerplate: analyst headers, certificates, etc."""
    import re as _re

    lines = text.split("\n")
    cleaned: list[str] = []
    skip_block = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned.append(line)
            continue

        # Detect start of boilerplate blocks
        if _re.match(
            r"^(分析师|研究员|金融工程|证书编号|S079\d{10}|相关研究|证券研究报告|"
            r"请务必参阅正文后面的信息披露|法律声明|免责声明|评级说明|"
            r"投资评级|重要声明|信息披露|分析师声明|评级定义)",
            stripped,
        ):
            skip_block = True
            continue

        # End of analyst block — resume when we see a real section header
        if skip_block:
            # Resume if line looks like a real section header (not boilerplate continuation)
            if _re.match(
                r"^[一二三四五六七八九十]+[、，.]|"
                r"^[（(][一二三四五六七八九十]+[）)]|"
                r"^(摘要|Abstract|引言|前言|结论|总结|背景|方法|数据|实证|回测|结果|分析|参考文献|附录|"
                r"筹码|反转|动量|因子|收益|风险|模型|策略)",
                stripped,
            ):
                skip_block = False
            else:
                continue

        cleaned.append(line)

    return "\n".join(cleaned)


def _build_summary_from_content(report, content: str) -> str:
    """Fallback: build a summary prompt from raw content (no analysis)."""
    # Strip brokerage report boilerplate before sending to LLM
    content = _strip_boilerplate(content)
    extra_hints = ""
    import json as _json

    if report.tables_json:
        try:
            tables = _json.loads(report.tables_json)
            extra_hints += (
                f"\n\n该研报包含 {len(tables)} 个表格，请在总结中提及关键表格数据。"
            )
        except Exception:
            pass
    if report.equations_json:
        try:
            equations = _json.loads(report.equations_json)
            extra_hints += f"\n\n该研报包含 {len(equations)} 个公式。"
        except Exception:
            pass

    return (
        "你是一位量化金融研究总结专家。请对以下量化金融研报撰写一份精炼的总结。\n\n"
        "【输出要求】\n"
        "- 以自然的叙述方式撰写，像一篇学术摘要一样流畅连贯。\n"
        "- 总结应自然涵盖：研究问题与核心结论、方法步骤与关键公式、因子构建逻辑、综合评估。\n"
        "- 不要使用分节标题，用自然的段落过渡。\n\n"
        "【强制格式要求 - 必须严格遵守】\n"
        "- 所有文本字段用中文输出。\n"
        "- 数学符号、变量名用 $...$ LaTeX 格式。\n"
        "- 完整公式用 $$...$$ 单独成行。\n"
        "- 严禁使用 \\(...\\) 或 \\[...\\] 格式。\n"
        f"{extra_hints}\n\n"
        f"## 研报标题\n{report.title}\n\n"
        f"## 研报内容\n{content[:12000]}"
    )


@router.post("/{report_id}/parse")
async def deep_parse_report(report_id: int, db: Session = Depends(get_db)):
    """MinerU 深度解析研报 PDF，提取表格和公式。"""
    repo = ReportRepository(db)
    report = repo.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    pdf_url = None
    if report.source == "arxiv" and report.source_url:
        arxiv_id = report.arxiv_id or report.source_url.split("/abs/")[-1]
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

    pdf_path = report.pdf_path

    if not pdf_path and not pdf_url:
        raise HTTPException(status_code=400, detail="No PDF available for this report")

    import asyncio
    from pathlib import Path

    async def do_parse():
        pdf_bytes = None

        if pdf_path and Path(pdf_path).exists():
            pdf_bytes = Path(pdf_path).read_bytes()
        elif pdf_url:
            import httpx

            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(pdf_url)
                resp.raise_for_status()
                pdf_bytes = resp.content

        if not pdf_bytes:
            return None

        return await asyncio.to_thread(_mineru_parse_bytes, pdf_bytes)

    result = await do_parse()
    if not result:
        raise HTTPException(status_code=500, detail="PDF parse failed")

    tables_data, equations_data, full_text = result

    # Normalize PDF extraction artifacts in formulas
    if equations_data:
        from reportagent.processors.formula_normalizer import normalize_formulas

        equations_data = normalize_formulas(equations_data)

    # Enrich equations with LLM explanations
    if equations_data:
        from reportagent.agents.analysis_agent import explain_equations
        from reportagent.llm.client import LLMClient

        try:
            client = LLMClient()
            equations_data = await explain_equations(
                equations=equations_data,
                title=report.title,
                context_text=full_text or report.abstract or "",
                llm_client=client,
            )
        except Exception:
            pass

    if tables_data:
        report.tables_json = json.dumps(tables_data, ensure_ascii=False)
    if equations_data:
        report.equations_json = json.dumps(equations_data, ensure_ascii=False)
    if full_text and len(full_text) > len(report.full_text or ""):
        report.full_text = full_text
        report.has_full_text = True
    db.commit()

    return {
        "success": True,
        "data": {
            "tables_count": len(tables_data),
            "equations_count": len(equations_data),
            "full_text_length": len(full_text) if full_text else 0,
        },
    }


def _mineru_parse_bytes(pdf_bytes: bytes):
    import tempfile
    from pathlib import Path

    try:
        from reportagent.processors.mineru_parser import MinerUParser, _run_mineru_parse

        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "report.pdf"
            pdf_path.write_bytes(pdf_bytes)
            content_list = _run_mineru_parse(str(pdf_path), tmp_dir)
            parser = MinerUParser()
            parsed = parser._build_result(content_list, str(pdf_path))
            return parsed.tables, parsed.equations, parsed.full_text_with_tables
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("MinerU deep parse failed: %s", e)

    try:
        import fitz

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            text = "\n".join(page.get_text() for page in doc)
            return [], [], text
        finally:
            doc.close()
    except Exception:
        pass

    return [], [], None


@router.post("/{report_id}/analyze")
async def analyze_report(
    report_id: int,
    depth: str = Query("standard", regex="^(quick|standard|deep)$"),
    db: Session = Depends(get_db),
):
    """深度分析研报：四阶段流水线（元数据→方法论→公式解读→综合评估）。"""
    repo = ReportRepository(db)
    report = repo.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    content = report.full_text or report.abstract or ""
    if not content:
        raise HTTPException(status_code=400, detail="No content to analyze")

    from datetime import datetime, timezone
    from reportagent.agents.analysis_agent import AnalysisAgent
    from reportagent.llm.client import LLMClient
    from reportagent.models.schemas import AnalysisResult

    client = LLMClient()
    agent = AnalysisAgent(llm_client=client)

    async def stream_analysis():
        phases_order = (
            ["metadata"]
            if depth == "quick"
            else ["metadata", "methodology", "assessment"]
        )

        yield f"data: {json.dumps({'type': 'start', 'depth': depth, 'phases': phases_order})}\n\n"

        # Manual iteration to interleave progress events between phases
        result = AnalysisResult(depth=depth)

        if "metadata" in phases_order:
            yield f"data: {json.dumps({'type': 'phase', 'phase': 'metadata', 'label': '提取元数据...'})}\n\n"
            result = await agent._run_phase_metadata(
                report.title, report.abstract or "", content, result
            )

        if "methodology" in phases_order:
            yield f"data: {json.dumps({'type': 'phase', 'phase': 'methodology', 'label': '方法论+公式深度解析...'})}\n\n"
            result = await agent._run_phase_methodology(
                report.title, content, result, report.equations_json
            )

        if "assessment" in phases_order:
            yield f"data: {json.dumps({'type': 'phase', 'phase': 'assessment', 'label': '综合评估...'})}\n\n"
            result = await agent._run_phase_assessment(report.title, content, result)

        yield f"data: {json.dumps({'type': 'phase', 'phase': 'summary', 'label': '生成总结...'})}\n\n"
        result.analyzed_at = datetime.now(timezone.utc).isoformat()
        result.summary = await agent._synthesize_summary(result, report.title)
        # Latexify post-process
        latexify_cfg = agent._config.get("analysis", {}).get("latexify", {})
        if latexify_cfg.get("enabled", True):
            result = await agent._latexify_result(result, report.title)

        analysis_json = result.model_dump_json()
        repo2 = ReportRepository(db)
        repo2.update_analysis(report_id, analysis_json)

        yield f"data: {json.dumps({'type': 'done', 'result': json.loads(analysis_json)})}\n\n"

    return StreamingResponse(stream_analysis(), media_type="text/event-stream")


# ── Knowledge Card ──────────────────────────────────────────────


@router.post("/{report_id}/knowledge-card")
def get_knowledge_card(
    report_id: int,
    force: bool = Query(False, description="刷新当前证据的卡片展示；不重新评估论文"),
    db: Session = Depends(get_db),
):
    from reportagent.services.knowledge_cards import get_knowledge_card as prepare_card

    report = ReportRepository(db).get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    previous = report.knowledge_card_json
    card = prepare_card(report, force=force)
    if report.knowledge_card_json != previous:
        db.commit()
    return {"success": True, "data": card}


# ── Per-Paper Ask AI ────────────────────────────────────────────


_ASK_SYSTEM_PROMPT = """你是一个量化金融论文问答助手。根据以下论文内容回答用户的问题。

论文标题：{title}
作者：{authors}

论文内容：
{content}

规则：
- 只能基于上述论文内容回答问题，不要引入外部知识
- 如果论文中没有相关信息，诚实说明"论文中未涉及此内容"
- 引用论文中的具体段落或数据来支撑你的回答
- 用中文回答
- 使用 Markdown 格式化，数学符号用 $...$ 格式"""


class AskRequest(BaseModel):
    question: str


@router.post("/{report_id}/ask")
async def ask_paper(
    report_id: int,
    req: AskRequest,
    db: Session = Depends(get_db),
):
    """针对单篇论文的问答（SSE 流式）。"""
    repo = ReportRepository(db)
    report = repo.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    content = report.full_text or report.abstract or ""
    if not content:
        raise HTTPException(status_code=400, detail="No content available")

    authors_str = ""
    if report.authors:
        try:
            authors_list = json.loads(report.authors)
            authors_str = ", ".join(authors_list[:5])
        except Exception:
            pass

    system_prompt = _ASK_SYSTEM_PROMPT.format(
        title=report.title,
        authors=authors_str,
        content=content[:12000],
    )

    from reportagent.llm.client import LLMClient

    async def stream_answer():
        client = LLMClient()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": req.question},
        ]
        try:
            async for chunk in client.chat_stream(
                messages, temperature=0.3, max_tokens=2000
            ):
                yield f"data: {json.dumps({'type': 'token', 'content': chunk}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(stream_answer(), media_type="text/event-stream")


@router.get("/{report_id}/pdf")
async def get_report_pdf(report_id: int, db: Session = Depends(get_db)):
    from pathlib import Path

    repo = ReportRepository(db)
    report = repo.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    if report.pdf_path and Path(report.pdf_path).exists():
        return FileResponse(
            report.pdf_path,
            media_type="application/pdf",
            filename=f"report_{report_id}.pdf",
            content_disposition_type="inline",
        )

    if report.source == "arxiv" and report.source_url:
        arxiv_id = report.arxiv_id or report.source_url.split("/abs/")[-1]
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

        import httpx

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(pdf_url)
            resp.raise_for_status()
            pdf_bytes = resp.content

        from reportagent.utils.config import PROJECT_ROOT

        pdf_dir = PROJECT_ROOT / "data" / "pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        safe_name = arxiv_id.replace("/", "_").replace("\\", "_")
        local_path = pdf_dir / f"{safe_name}.pdf"
        local_path.write_bytes(pdf_bytes)

        report.pdf_path = str(local_path)
        db.commit()

        return FileResponse(
            str(local_path),
            media_type="application/pdf",
            filename=f"report_{report_id}.pdf",
            content_disposition_type="inline",
        )

    raise HTTPException(status_code=404, detail="No PDF available for this report")


@router.delete("/{report_id}")
async def delete_report(report_id: int, db: Session = Depends(get_db)):
    repo = ReportRepository(db)
    if not repo.delete_report(report_id):
        raise HTTPException(status_code=404, detail="Report not found")
    return {"success": True, "message": "Report deleted"}
