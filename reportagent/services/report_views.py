"""Shared report serialization and analysis presentation."""
import json
from reportagent.services.paper_scoring import relevance_reason_text

def report_to_dict(r) -> dict:
    from reportagent.services.knowledge_cards import report_snapshot
    from reportagent.services.report_quality import current_relevance
    paper = report_snapshot(r)
    custom_tags = [t.value for t in r.tags if t.dimension == "custom"] if r.tags else []
    return {
        "id": r.id, "title": r.title,
        "authors": json.loads(r.authors) if r.authors else [],
        "abstract": r.abstract, "source": r.source, "source_url": r.source_url,
        "doi": r.doi, "arxiv_id": r.arxiv_id,
        "published_date": r.published_date.isoformat() if r.published_date else None,
        "publication_date_info": json.loads(r.publication_date_info_json) if r.publication_date_info_json else {},
        "has_full_text": r.has_full_text, "pdf_path": r.pdf_path,
        "markets": r.markets.split(",") if r.markets else [],
        "asset_classes": r.asset_classes.split(",") if r.asset_classes else [],
        "frequencies": r.frequencies.split(",") if r.frequencies else [],
        "topics": r.topics.split(",") if r.topics else [],
        "custom_tags": custom_tags, "content_hash": r.content_hash,
        "classification_provenance": json.loads(r.classification_provenance_json) if r.classification_provenance_json else {},
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        "summary": r.summary,
        "selection_relevance": current_relevance(paper),
        "selection_direction": r.selection_direction,
        "selection_reason": relevance_reason_text(r.selection_reason),
        "evaluation": read_evaluation(r.evaluation_json, paper=paper),
    }


def read_evaluation(value, *, paper: dict | None = None) -> dict | None:
    try:
        parsed = json.loads(value) if value else None
        if not isinstance(parsed, dict):
            return None
        if 'relevance_reason' in parsed:
            parsed['relevance_reason'] = relevance_reason_text(parsed['relevance_reason'])
        from reportagent.services.report_quality import SCHEMA_VERSION, pending_review, review_fingerprint, cached_review_is_current
        if paper is not None:
            from reportagent.utils.config import get_llm_model
            model = get_llm_model()
            same_context = (parsed.get('schema_version') == SCHEMA_VERSION
                            and parsed.get('model') == model
                            and parsed.get('fingerprint') == review_fingerprint(paper, model))
            current_pending = (same_context and parsed.get('tier') == 'pending'
                               and parsed.get('status') in {'pending', 'needs_verification'})
            current = current_pending or cached_review_is_current(paper, parsed, model)
            if not current:
                pending = pending_review(paper, '原文、模型或筛选标准已变更，或尚无有效全文评估；请重新评估。')
                pending['relevance_reason'] = relevance_reason_text(pending['relevance_reason'])
                # A GET is not a new assessment: keep the historical time.
                pending['reviewed_at'] = parsed.get('reviewed_at')
                return pending
        if parsed.get('schema_version') in range(1, SCHEMA_VERSION):
            return {**parsed, 'tier': 'pending', 'label': '待重新评估', 'status': 'pending',
                    'validation_issues': ['评估规则已更新，请重新生成全文阅读评估。']}
        return parsed
    except (TypeError, ValueError):
        return None

def map_analysis_to_knowledge_card(analysis: dict) -> dict:
    """Map existing AnalysisResult dict to KnowledgeCard fields."""
    methodology = analysis.get("methodology") or {}
    assessment = analysis.get("assessment") or {}

    highlights = list(assessment.get("key_contributions", [])[:3])
    if not highlights:
        highlights = assessment.get("strengths", [])[:3]

    steps = []
    for pt in methodology.get("analysis_points", []):
        if isinstance(pt, dict):
            step_text = pt.get("analysis", "") or pt.get("title", "")
            if step_text:
                steps.append(step_text)

    return {
        "summary": assessment.get("marginal_contribution_summary", "")
                   or analysis.get("core_contribution", ""),
        "highlights": highlights,
        "methodology_steps": steps,
        "results": analysis.get("summary", ""),
        "marginal_contributions": assessment.get("marginal_contribution_summary", ""),
        "implications": assessment.get("practical_implications", []),
        "topics": [],
        "asset_classes": [],
        "quality_score": assessment.get("overall_quality_score"),
    }

def map_analysis_to_briefing_sections(analysis: dict) -> list[dict]:
    """Map existing AnalysisResult to briefing sections (fallback)."""
    methodology = analysis.get("methodology") or {}
    assessment = analysis.get("assessment") or {}
    data_used = analysis.get("data_used") or {}

    sections = []

    # 1) 研究背景与问题
    problem_parts = []
    if analysis.get("research_question"):
        problem_parts.append(f"**研究问题：** {analysis['research_question']}")
    if analysis.get("core_contribution"):
        problem_parts.append(f"**核心创新：** {analysis['core_contribution']}")
    if data_used:
        parts = []
        if data_used.get("market"): parts.append(f"市场：{data_used['market']}")
        if data_used.get("sample_period"): parts.append(f"样本期：{data_used['sample_period']}")
        if data_used.get("frequency"): parts.append(f"频率：{data_used['frequency']}")
        if data_used.get("universe"): parts.append(f"股票池：{data_used['universe']}")
        if parts:
            problem_parts.append(f"**数据：** {' | '.join(parts)}")
    if analysis.get("benchmark_models"):
        problem_parts.append(f"**基准模型：** {', '.join(analysis['benchmark_models'])}")
    if problem_parts:
        sections.append({"title": "研究背景与问题", "icon": "🎯", "content": "\n\n".join(problem_parts)})

    # 2) 核心方法
    method_parts = []
    for pt in methodology.get("analysis_points", []):
        if isinstance(pt, dict):
            title = pt.get("title", "")
            analysis_text = pt.get("analysis", "")
            if analysis_text:
                method_parts.append(f"### {title}\n\n{analysis_text}")
    factors = methodology.get("factor_list", [])
    if factors:
        fl = "\n".join(
            f"- **{f.get('name', '')}**（{f.get('type', '')}）：{f.get('construction', '')}"
            for f in factors
        )
        method_parts.append(f"**因子清单：**\n{fl}")
    if method_parts:
        sections.append({"title": "核心方法", "icon": "🔬", "content": "\n\n".join(method_parts)})

    # 3) 关键创新点
    innov_parts = []
    if assessment.get("key_contributions"):
        innov_parts.append("**核心贡献：**\n" + "\n".join(f"- {c}" for c in assessment["key_contributions"]))
    if assessment.get("marginal_contribution_summary"):
        innov_parts.append(f"**边际贡献：**\n{assessment['marginal_contribution_summary']}")
    if innov_parts:
        sections.append({"title": "关键创新点", "icon": "💡", "content": "\n\n".join(innov_parts)})

    # 4) 实证检验
    emp_parts = []
    if assessment.get("overall_quality_score") is not None:
        emp_parts.append(f"**综合评分：** {assessment['overall_quality_score'] * 10:.1f} / 10")
    if assessment.get("strengths"):
        emp_parts.append("**优势：**\n" + "\n".join(f"- {s}" for s in assessment["strengths"]))
    if assessment.get("weaknesses"):
        emp_parts.append("**不足：**\n" + "\n".join(f"- {w}" for w in assessment["weaknesses"]))
    if emp_parts:
        sections.append({"title": "实证检验与评估", "icon": "📊", "content": "\n\n".join(emp_parts)})

    # 5) 实践启示
    if assessment.get("practical_implications"):
        sections.append({
            "title": "实践启示",
            "icon": "🔮",
            "content": "\n\n".join(f"{i+1}. {imp}" for i, imp in enumerate(assessment["practical_implications"])),
        })

    # 6) 总结
    if analysis.get("summary"):
        sections.append({"title": "总结", "icon": "📌", "content": analysis["summary"]})

    return sections
