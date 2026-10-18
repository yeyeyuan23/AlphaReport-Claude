"""Shared report serialization and analysis presentation."""

import json
from reportagent.services.paper_scoring import relevance_reason_text


def read_current_analysis(report) -> dict | None:
    """Hide outdated batch results, while retaining historical unsigned formats."""
    try:
        analysis = json.loads(report.analysis_json or "null")
        if not isinstance(analysis, dict):
            return None
        if "analysis_fingerprint" in analysis:
            from reportagent.services.batch_analysis import analysis_cache_fingerprint

            depth = analysis.get("depth")
            if depth not in {"quick", "standard", "deep"} or analysis.get(
                "analysis_fingerprint"
            ) != analysis_cache_fingerprint(report, depth):
                return None
        return analysis
    except (TypeError, ValueError):
        return None


def report_to_dict(r) -> dict:
    from reportagent.services.knowledge_cards import report_snapshot
    from reportagent.services.report_quality import current_relevance

    paper = report_snapshot(r)
    custom_tags = [t.value for t in r.tags if t.dimension == "custom"] if r.tags else []
    return {
        "id": r.id,
        "title": r.title,
        "authors": json.loads(r.authors) if r.authors else [],
        "abstract": r.abstract,
        "source": r.source,
        "material_type": r.material_type or "unknown",
        "material_type_info": json.loads(r.material_type_info_json)
        if r.material_type_info_json
        else {},
        "source_url": r.source_url,
        "doi": r.doi,
        "arxiv_id": r.arxiv_id,
        "published_date": r.published_date.isoformat() if r.published_date else None,
        "publication_date_info": json.loads(r.publication_date_info_json)
        if r.publication_date_info_json
        else {},
        "has_full_text": r.has_full_text,
        "pdf_path": r.pdf_path,
        "markets": r.markets.split(",") if r.markets else [],
        "asset_classes": r.asset_classes.split(",") if r.asset_classes else [],
        "frequencies": r.frequencies.split(",") if r.frequencies else [],
        "topics": r.topics.split(",") if r.topics else [],
        "custom_tags": custom_tags,
        "content_hash": r.content_hash,
        "classification_provenance": json.loads(r.classification_provenance_json)
        if r.classification_provenance_json
        else {},
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
        if "relevance_reason" in parsed:
            parsed["relevance_reason"] = relevance_reason_text(
                parsed["relevance_reason"]
            )
        from reportagent.services.report_quality import (
            SCHEMA_VERSION,
            pending_review,
            review_fingerprint,
            cached_review_is_current,
        )

        if paper is not None:
            from reportagent.utils.config import get_llm_model

            model = get_llm_model()
            same_context = (
                parsed.get("schema_version") == SCHEMA_VERSION
                and parsed.get("model") == model
                and parsed.get("fingerprint") == review_fingerprint(paper, model)
            )
            current_pending = (
                same_context
                and parsed.get("tier") == "pending"
                and parsed.get("status") in {"pending", "needs_verification"}
            )
            current = current_pending or cached_review_is_current(paper, parsed, model)
            if not current:
                pending = pending_review(
                    paper,
                    "原文、模型或筛选标准已变更，或尚无有效全文评估；请重新评估。",
                )
                pending["relevance_reason"] = relevance_reason_text(
                    pending["relevance_reason"]
                )
                # A GET is not a new assessment: keep the historical time.
                pending["reviewed_at"] = parsed.get("reviewed_at")
                return pending
        if parsed.get("schema_version") in range(1, SCHEMA_VERSION):
            return {
                **parsed,
                "tier": "pending",
                "label": "待重新评估",
                "status": "pending",
                "validation_issues": ["评估规则已更新，请重新生成全文阅读评估。"],
            }
        return parsed
    except (TypeError, ValueError):
        return None


def map_analysis_to_briefing_sections(analysis: dict) -> list[dict]:
    """Canonical deterministic presentation of saved analysis for every web view."""
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
        if data_used.get("market"):
            parts.append(f"市场：{data_used['market']}")
        if data_used.get("sample_period"):
            parts.append(f"样本期：{data_used['sample_period']}")
        if data_used.get("frequency"):
            parts.append(f"频率：{data_used['frequency']}")
        if data_used.get("universe"):
            parts.append(f"股票池：{data_used['universe']}")
        if data_used.get("instruments"):
            parts.append(f"工具：{'、'.join(data_used['instruments'])}")
        if parts:
            problem_parts.append(f"**数据：** {' | '.join(parts)}")
    if analysis.get("benchmark_models"):
        problem_parts.append(
            f"**基准模型：** {'、'.join(analysis['benchmark_models'])}"
        )
    if problem_parts:
        sections.append(
            {
                "title": "研究背景与问题",
                "icon": "🎯",
                "content": "\n\n".join(problem_parts),
            }
        )

    # 2) 核心方法
    method_parts = []
    for pt in methodology.get("analysis_points") or []:
        if isinstance(pt, dict):
            title = pt.get("title", "")
            analysis_text = pt.get("analysis", "")
            if analysis_text:
                method_parts.append(f"### {title}\n\n{analysis_text}")
    factors = methodology.get("factor_list", [])
    if factors:
        fl = "\n".join(
            f"- **{f.get('name', '')}**（{f.get('type', '')}）：{f.get('construction', '')}"
            + (f" [{f['raw_or_neutralized']}]" if f.get("raw_or_neutralized") else "")
            for f in factors
        )
        method_parts.append(f"**因子清单：**\n{fl}")
    architecture = methodology.get("model_architecture") or {}
    if architecture:
        lines = [f"**模型架构：** {architecture.get('type', '')}"]
        for field, label in (
            ("layers_or_structure", "结构"),
            ("loss_function", "损失函数"),
            ("regularization", "正则化"),
            ("training_scheme", "训练方式"),
        ):
            if architecture.get(field):
                value = architecture[field]
                if field == "loss_function":
                    value = f"${value}$"
                lines.append(f"- {label}：{value}")
        method_parts.append("\n".join(lines))
    portfolio = methodology.get("portfolio_construction") or {}
    if portfolio:
        lines = ["**组合构建：**"]
        for field, label in (
            ("weighting", "加权方式"),
            ("rebalance_frequency", "再平衡频率"),
            ("constraints", "约束条件"),
            ("transaction_cost_model", "交易成本"),
        ):
            if portfolio.get(field):
                lines.append(f"- {label}：{portfolio[field]}")
        method_parts.append("\n".join(lines))
    key_formulas = [
        f"**核心公式：**\n$${equation.get('latex', '')}$$\n\n{equation.get('meaning', '')}"
        for equation in analysis.get("equations") or []
        if equation.get("is_key_formula")
    ]
    if key_formulas:
        method_parts.append("---\n\n" + "\n\n".join(key_formulas))
    if method_parts:
        sections.append(
            {"title": "核心方法", "icon": "🔬", "content": "\n\n".join(method_parts)}
        )

    # 3) 关键创新点
    innov_parts = []
    if assessment.get("key_contributions"):
        innov_parts.append(
            "**核心贡献：**\n"
            + "\n".join(f"- {c}" for c in assessment["key_contributions"])
        )
    if assessment.get("marginal_contribution_summary"):
        innov_parts.append(
            f"**边际贡献：**\n{assessment['marginal_contribution_summary']}"
        )
    if innov_parts:
        sections.append(
            {"title": "关键创新点", "icon": "💡", "content": "\n\n".join(innov_parts)}
        )

    # 4) 实证检验
    emp_parts = []
    if assessment.get("overall_quality_score") is not None:
        emp_parts.append(
            f"**综合评分：** {assessment['overall_quality_score'] * 10:.1f} / 10"
        )
    if assessment.get("strengths"):
        emp_parts.append(
            "**优势：**\n" + "\n".join(f"- {s}" for s in assessment["strengths"])
        )
    if assessment.get("weaknesses"):
        emp_parts.append(
            "**不足：**\n" + "\n".join(f"- {w}" for w in assessment["weaknesses"])
        )
    bias_risks = assessment.get("bias_risks") or {}
    if bias_risks:
        labels = {
            "none": "无风险 ✅",
            "low": "低风险 ⚠️",
            "medium": "中风险 ⚠️",
            "high": "高风险 ❌",
            "unknown": "未知 ❓",
        }
        lines = []
        for field, label in (
            ("look_ahead_bias", "前视偏差"),
            ("survivorship_bias", "幸存者偏差"),
            ("data_snooping", "数据窥探"),
        ):
            if bias_risks.get(field):
                value = bias_risks[field]
                lines.append(f"- {label}：{labels.get(value, value)}")
        if bias_risks.get("overfitting_concern") not in (None, "", "none"):
            lines.append(f"- 过拟合风险：{bias_risks['overfitting_concern']}")
        if lines:
            emp_parts.append("**偏差风险评估：**\n" + "\n".join(lines))
    reproducibility = assessment.get("reproducibility") or {}
    if reproducibility:
        text = f"**可复现性：** {reproducibility.get('level', '')}"
        if reproducibility.get("has_data_source"):
            text += " · 有数据源"
        if reproducibility.get("has_code_available"):
            text += " · 有代码"
        emp_parts.append(text)
    applicability = assessment.get("a_share_applicability") or {}
    if applicability:
        status = (
            "可直接应用 ✅"
            if applicability.get("directly_applicable")
            else "需要适配 ⚠️"
        )
        emp_parts.append(f"**A股适用性：** {status}")
        if applicability.get("adaptations_needed"):
            emp_parts.append(
                f"适配需求：{'；'.join(applicability['adaptations_needed'])}"
            )
        if applicability.get("key_constraints"):
            emp_parts.append(f"关键限制：{'；'.join(applicability['key_constraints'])}")
    if emp_parts:
        sections.append(
            {"title": "实证检验与评估", "icon": "📊", "content": "\n\n".join(emp_parts)}
        )

    # 5) 实践启示
    if assessment.get("practical_implications"):
        sections.append(
            {
                "title": "实践启示",
                "icon": "🔮",
                "content": "\n\n".join(
                    f"{i + 1}. {imp}"
                    for i, imp in enumerate(assessment["practical_implications"])
                ),
            }
        )

    # 6) 总结
    if analysis.get("summary"):
        sections.append({"title": "总结", "icon": "📌", "content": analysis["summary"]})

    return sections


def read_saved_reviews(report_ids: list[int]) -> dict[int, dict]:
    """Read current stored assessments without model calls or material fetching."""
    from reportagent.db.engine import get_session_factory
    from reportagent.models.database import Report
    from reportagent.services.report_quality import pending_review

    with get_session_factory()() as session:
        rows = session.query(Report).filter(Report.id.in_(report_ids)).all()
        return {
            row.id: read_evaluation(row.evaluation_json, paper=vars(row))
            or pending_review(vars(row), "后台全文评估尚未完成。")
            for row in rows
        }
