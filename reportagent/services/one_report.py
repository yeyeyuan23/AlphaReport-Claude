"""Explicit, full-text-only arXiv -> analysis -> private Feishu delivery."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from reportagent.agents.analysis_agent import AnalysisAgent
from reportagent.classifiers.rule_classifier import RuleClassifier
from reportagent.db.engine import get_session_factory, init_db
from reportagent.db.repository import ReportRepository
from reportagent.llm.client import LLMClient
from reportagent.models.schemas import AnalysisResult, ClassifiedReport, SearchResult, SourceType
from reportagent.models.database import Report
from reportagent.sources.arxiv_source import ArxivSource
from reportagent.utils.config import PROJECT_ROOT, get_db_path, get_llm_model, get_llm_base_url
from reportagent.skills.registry import skill_resource
from reportagent.services.public_url import get_web_base_url

logger = logging.getLogger(__name__)


def analysis_fingerprint() -> str:
    """Model and task resources are part of an analysis, not just the PDF bytes."""
    from reportagent.services.report_quality import SCHEMA_VERSION
    resources = {name: skill_resource("analyze-report", name).read_text(encoding="utf-8")
                 for name in ("one-report.yaml", "summary.yaml", "quality-review.md", "grounding.md", "single-brief.md")}
    content = {"version": 3, "quality_schema": SCHEMA_VERSION, "model": get_llm_model(), "endpoint": get_llm_base_url(),
               "thinking": "disabled", "resources": resources}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalize_arxiv_id(value: str) -> str:
    value = re.sub(r"^https://arxiv\.org/(abs|pdf)/", "", value.strip())
    value = re.sub(r"\.pdf$", "", value)
    if not re.fullmatch(r"\d{4}\.\d{4,5}(?:v[1-9]\d*)?", value):
        raise ValueError("请输入现代 arXiv 编号，例如 2005.13665，或其 arxiv.org 链接")
    return value


async def collect_paper(arxiv_id: str) -> SearchResult:
    arxiv_id = normalize_arxiv_id(arxiv_id)
    # An explicit immutable revision can be reused during analysis retries.
    # Unversioned requests still resolve the latest revision from arXiv.
    if "v" in arxiv_id and get_db_path().exists():
        with get_session_factory()() as session:
            stored = session.query(Report).filter(Report.arxiv_id == arxiv_id).first()
            pdf_path = PROJECT_ROOT / "data" / "pdfs" / f"{arxiv_id}.pdf"
            if stored and pdf_path.exists() and stored.source_url == f"https://arxiv.org/abs/{arxiv_id}":
                text = await asyncio.to_thread(ArxivSource._parse_pdf_bytes, pdf_path.read_bytes())
                if text and len(text.strip()) >= 1000:
                    logger.info("Reusing collected PDF revision: %s", arxiv_id)
                    return SearchResult(title=stored.title, authors=json.loads(stored.authors),
                        abstract=stored.abstract, full_text=text, abstract_only=False,
                        source=SourceType.ARXIV, source_url=stored.source_url, arxiv_id=arxiv_id,
                        published_date=stored.published_date, pdf_path=str(pdf_path),
                        raw_metadata={"pdf_url": f"https://arxiv.org/pdf/{arxiv_id}"})
    url = f"https://arxiv.org/abs/{arxiv_id}"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        def meta(name):
            tag = soup.find("meta", attrs={"name": name})
            return tag.get("content", "").strip() if tag else ""

        title = meta("citation_title")
        authors = [tag.get("content", "") for tag in soup.find_all("meta", attrs={"name": "citation_author"})]
        abstract_tag = soup.select_one("blockquote.abstract")
        abstract = re.sub(r"^Abstract:\s*", "", abstract_tag.get_text(" ", strip=True)) if abstract_tag else ""
        if not title or not abstract or not authors:
            raise ValueError("arXiv 页面缺少标题、作者或摘要，停止采集")
        # Pin the downloaded PDF to the version shown on the official metadata page.
        version_tag = soup.find("meta", attrs={"name": "citation_arxiv_id"})
        version = version_tag.get("content", "") if version_tag else ""
        version_match = re.search(r"arXiv:(\d{4}\.\d{4,5}v\d+)", soup.get_text(" "))
        if "v" in arxiv_id:
            version = arxiv_id
        elif version_match:
            version = version_match.group(1)
        version = normalize_arxiv_id(version or arxiv_id)
        if version.split("v")[0] != arxiv_id.split("v")[0]:
            raise ValueError("arXiv 页面编号不匹配")
        pdf_url = f"https://arxiv.org/pdf/{version}"
        date_text = meta("citation_date")
        published = datetime.strptime(date_text, "%Y/%m/%d") if date_text else None
        paper = SearchResult(
            title=title, authors=authors, abstract=abstract, source=SourceType.ARXIV,
            source_url=f"https://arxiv.org/abs/{version}", arxiv_id=version,
            published_date=published, abstract_only=True,
            raw_metadata={"pdf_url": pdf_url},
        )
        paper = await ArxivSource()._fetch_one_pdf(client, paper, pdf_url)
    if paper.abstract_only or not paper.full_text or len(paper.full_text.strip()) < 1000:
        raise ValueError("未取得可用 PDF 全文，停止分析")
    return paper


def validate_analysis(data: dict) -> AnalysisResult:
    result = AnalysisResult.model_validate(data)
    if not all([result.research_question.strip(), result.core_contribution.strip(), result.summary.strip()]):
        raise ValueError("分析不完整：研究问题、核心贡献或总结为空")
    if not result.methodology or not any(p.get("analysis", "").strip() for p in result.methodology.analysis_points):
        raise ValueError("分析不完整：方法论为空")
    if not result.assessment or not result.assessment.strengths or not result.assessment.weaknesses:
        raise ValueError("分析不完整：优势或局限评估为空")
    return result


def make_brief(paper: SearchResult, result: AnalysisResult, model: str,
               evaluation: dict | None = None, *, report_id: int | None = None) -> str:
    from reportagent.services.report_briefing import evaluation_sections, render_single_brief

    if evaluation:
        sections = evaluation_sections(evaluation)
    else:
        method = "\n".join(f"• {p.get('title', '')}：{p['analysis']}"
                           for p in result.methodology.analysis_points[:3])
        sections = {
            "label": "尚未按新标准评估",
            "research_question": result.research_question,
            "method_findings": f"{method}\n\n{result.summary}",
            "strengths": "\n".join([result.core_contribution, *result.assessment.strengths[:3]]),
            "limitations": "\n".join(f"• {x}" for x in result.assessment.weaknesses[:3]),
            "critical_concerns": "历史分析未单独记录关键疑虑。",
            "reading_advice": "参照历史分析并核对原文；新版全文评估暂无记录。",
            "verification": "历史分析尚未按新标准核验原文引证。",
        }
    base_url = get_web_base_url()
    brief = render_single_brief({
        "title": paper.title, "authors": paper.authors, "source_url": paper.source_url,
        "pdf_url": paper.raw_metadata.get("pdf_url"),
        "ui_url": f"{base_url.rstrip('/')}/library/{report_id}" if report_id is not None else None,
    }, sections, model)
    if len(brief.encode("utf-8")) > 28000:
        raise ValueError("分析简报超过单条消息限制，请缩短后再推送")
    return brief


def save_artifact(path: Path, artifact: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def store_paper(paper: SearchResult, result: AnalysisResult | None = None, evaluation: dict | None = None) -> int:
    """An explicit user-requested revision replaces the library snapshot coherently."""
    from reportagent.services.report_views import map_analysis_to_knowledge_card as _map_analysis_to_knowledge_card
    init_db()
    with get_session_factory()() as session:
        repo = ReportRepository(session)
        _, report_id = repo.upsert_report(ClassifiedReport(
            search_result=paper,
            classification=RuleClassifier().classify(paper.title, paper.full_text),
        ))
        stored = repo.get_report(report_id)
        # Background collection never downgrades revisions. Explicit single-paper
        # requests may restore an older one, including all metadata and derived state.
        if any(getattr(stored, field) != getattr(paper, field) for field in ("arxiv_id", "title", "abstract", "full_text")):
            stored.selection_provenance_json = stored.selection_direction = stored.selection_relevance = stored.selection_reason = None
            stored.selection_score, stored.is_selected = 0.0, False
            stored.analysis_json = stored.evaluation_json = stored.knowledge_card_json = stored.summary = None
        for field in ("title", "full_text", "abstract", "arxiv_id", "source_url", "pdf_path", "published_date", "tables_json", "equations_json"):
            setattr(stored, field, getattr(paper, field))
        stored.authors = json.dumps(paper.authors, ensure_ascii=False)
        if result is not None:
            stored.analysis_json = result.model_dump_json()
            stored.summary = result.summary
            stored.knowledge_card_json = json.dumps(_map_analysis_to_knowledge_card(result.model_dump()), ensure_ascii=False)
        if evaluation is not None:
            stored.evaluation_json = json.dumps(evaluation, ensure_ascii=False)
        stored.has_full_text = bool(paper.full_text) and not paper.abstract_only
        session.commit()
    return report_id


def _stored_review_input(report_id: int) -> dict:
    with get_session_factory()() as session:
        row = session.get(Report, report_id)
        if row is None:
            raise ValueError("论文已不存在，请重试")
        return {key: getattr(row, key) for key in (
            "title", "source", "abstract", "arxiv_id", "full_text", "has_full_text",
            "selection_score", "selection_relevance", "selection_reason", "selection_provenance_json")}


async def prepare_report(arxiv_id: str, *, force: bool = False) -> tuple[Path, dict]:
    logger.info("Collecting arXiv %s", normalize_arxiv_id(arxiv_id))
    paper = await collect_paper(arxiv_id)
    logger.info("Full text acquired: %s characters", len(paper.full_text))
    path = PROJECT_ROOT / "data" / "one_reports" / f"{paper.arxiv_id}.json"
    pdf_hash = hashlib.sha256(Path(paper.pdf_path).read_bytes()).hexdigest()
    fingerprint = analysis_fingerprint()
    report_id = store_paper(paper)
    review_input = _stored_review_input(report_id)
    previous_deliveries = []
    cached_result, cached_limits, cached_calls = None, {}, []
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        previous_deliveries = cached.get("deliveries", [])
        if not force and cached.get("pdf_sha256") == pdf_hash and cached.get("analysis_fingerprint") == fingerprint:
            result = validate_analysis(cached["analysis"])
            evaluation = cached.get("evaluation") or {}
            from reportagent.services.report_quality import review_fingerprint
            if (evaluation.get("status") == "reviewed" and evaluation.get("tier") != "pending"
                    and evaluation.get("fingerprint") == review_fingerprint(review_input, get_llm_model())):
                cached["report_id"] = store_paper(paper, result, evaluation)
                save_artifact(path, cached)
                logger.info("Reusing completed analysis and delivery receipts: %s", path)
                return path, cached
            cached_result = result
            cached_limits = cached.get("phase_input_chars", {})
            cached_calls = cached.get("model_calls", [])
    llm = LLMClient(thinking="disabled")
    limits = cached_limits

    async def progress(phase, message):
        logger.info("Analysis stage: %s (%s)", phase, message)

    try:
        if cached_result is None:
            agent = AnalysisAgent(llm_client=llm, config_path=str(skill_resource("analyze-report", "one-report.yaml")))
            for name, cfg in agent._config["analysis"]["phases"].items():
                cfg["model"] = "default"  # Respect this environment's configured model in every phase.
                if cfg.get("enabled", True):
                    limits[name] = len(paper.full_text)
            result = await asyncio.wait_for(agent.analyze_report_directly(
                title=paper.title, abstract=paper.abstract, full_text=paper.full_text,
                depth="standard", progress_cb=progress,
            ), timeout=900)
        else:
            result = cached_result
        from reportagent.services.report_quality import evaluate_paper
        evaluation = await asyncio.wait_for(evaluate_paper(review_input, llm), timeout=240)
    finally:
        await llm._client.close()
    validate_analysis(result.model_dump())
    if any(not call["complete"] for call in llm.calls):
        raise ValueError("存在失败或截断的模型调用，停止推送")
    if _stored_review_input(report_id) != review_input:
        raise ValueError("分析期间原文或相关性结果已更新，请重新评估")
    report_id = store_paper(paper, result, evaluation)
    artifact = {
        "report_id": report_id, "arxiv_id": paper.arxiv_id, "title": paper.title,
        "source_url": paper.source_url, "pdf_path": paper.pdf_path,
        "pdf_sha256": pdf_hash,
        "analysis_fingerprint": fingerprint, "evaluation": evaluation,
        "full_text_chars": len(paper.full_text), "phase_input_chars": limits,
        "model": llm.model, "thinking": llm.thinking, "model_calls": cached_calls + llm.calls,
        "analysis": result.model_dump(),
        "brief": make_brief(paper, result, llm.model, evaluation, report_id=report_id), "deliveries": previous_deliveries,
    }
    save_artifact(path, artifact)
    path.with_suffix(".md").write_text(artifact["brief"], encoding="utf-8")
    logger.info("Analysis saved: %s", path)
    return path, artifact


def _send_to_chat(chat_id: str, text: str, request_id: str) -> str:
    from reportagent.services.feishu_service import _build_client
    from reportagent.services.feishu_replies import deliver_response
    message_ids = deliver_response(_build_client(), text, chat_id, request_id=request_id, title="单篇研报分析")
    if not message_ids:
        raise RuntimeError("Empty report cannot be delivered")
    return message_ids[-1]


def push_artifact(artifact: dict, chat_id: str) -> dict:
    validate_analysis(artifact["analysis"])
    if not re.fullmatch(r"oc_[A-Za-z0-9]+", chat_id):
        raise ValueError("必须指定有效的飞书会话 ID")
    deliveries = artifact.setdefault("deliveries", [])
    for receipt in deliveries:
        if receipt["chat_id"] == chat_id:
            return receipt
    # Stable UUID makes immediate retries safe even if saving the receipt fails.
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, chat_id + artifact["brief"]))
    message_id = _send_to_chat(chat_id, artifact["brief"], request_id)
    receipt = {"chat_id": chat_id, "message_id": message_id,
               "sent_at": datetime.now(timezone.utc).isoformat()}
    deliveries.append(receipt)
    return receipt
