"""Evidence-preserving summary construction, execution and guarded persistence."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass

import yaml
from reportagent.skills.registry import skill_resource
from reportagent.utils.config import get_llm_base_url, get_llm_model, get_llm_provider


@dataclass(frozen=True)
class SummaryRequest:
    messages: list[dict[str, str]]
    max_tokens: int
    temperature: float
    policy_fingerprint: str
    config: dict | None = None


def _load_config() -> dict:
    return yaml.safe_load(
        skill_resource("analyze-report", "summary.yaml").read_text(encoding="utf-8")
    )


def _grounding() -> str:
    return skill_resource("analyze-report", "grounding.md").read_text(encoding="utf-8")


def _policy_fingerprint(config: dict | None = None) -> str:
    policy = {
        "config": config if config is not None else _load_config(),
        "grounding": _grounding(),
        "model": get_llm_model(),
        "provider": get_llm_provider(),
        "endpoint": get_llm_base_url(),
    }
    return hashlib.sha256(
        json.dumps(policy, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _structured_context(analysis: dict) -> str:
    """Keep familiar readable labels and every supplied substantive analysis field."""
    parts = [
        f"研究问题：{analysis.get('research_question', '')}",
        f"核心贡献：{analysis.get('core_contribution', '')}",
    ]
    methodology = analysis.get("methodology") or {}
    equations = analysis.get("equations") or []
    points = methodology.get("analysis_points") or []
    for point in points:
        if not isinstance(point, dict):
            continue
        parts.append(f"**{point.get('title', '')}**：{point.get('analysis', '')}")
        for field, label in (
            ("marginal_contribution", "边际贡献"),
            ("practical_implication", "实践推论"),
        ):
            if point.get(field):
                parts.append(f"{label}：{point[field]}")
        for index in point.get("related_formulas", []):
            if type(index) is int and 0 <= index < len(equations):
                eq = equations[index]
                parts.append(
                    f"对应公式 ${eq.get('latex', '')}$（{eq.get('meaning', '')}）"
                )
    if not points:
        for eq in equations:
            if isinstance(eq, dict) and eq.get("latex"):
                parts.append(f"公式：${eq['latex']}$ — {eq.get('meaning', '')}")
    assessment = analysis.get("assessment") or {}
    if assessment.get("overall_quality_score") is not None:
        parts.append(f"综合评分：{assessment['overall_quality_score']}")
    # The full structured input preserves conditions, architecture, data, baselines,
    # formulas and legacy extra fields; the previous summaries silently omitted them.
    substantive = {
        key: value
        for key, value in analysis.items()
        if key not in {"summary", "model_calls", "analysis_fingerprint", "analyzed_at"}
    }
    parts.append(
        "完整结构化证据：\n" + json.dumps(substantive, ensure_ascii=False, indent=2)
    )
    return "\n\n".join(parts)


def _has_evidence(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip()) and value != "unknown"
    if isinstance(value, dict):
        return any(_has_evidence(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_evidence(item) for item in value)
    return False


def build_summary_request(
    title: str,
    *,
    analysis=None,
    content: str | None = None,
    depth: str = "standard",
    config: dict | None = None,
) -> SummaryRequest:
    """Use the shared skill/profile, refusing oversized input without truncation."""
    cfg = deepcopy(config) if config is not None else _load_config()
    if depth not in cfg.get("profiles", {}):
        raise ValueError(
            "depth must be quick, standard or deep with a configured profile"
        )
    if (analysis is None) == (content is None):
        raise ValueError("摘要必须且只能提供结构化分析或原文材料之一")
    if analysis is not None:
        data = analysis.model_dump() if hasattr(analysis, "model_dump") else analysis
        if not isinstance(data, dict) or not _has_evidence(
            {
                key: value
                for key, value in data.items()
                if key
                not in {
                    "depth",
                    "summary",
                    "analyzed_at",
                    "model_calls",
                    "analysis_fingerprint",
                    "evidence_scope",
                }
            }
        ):
            raise ValueError("摘要分析材料为空")
        context = _structured_context(data)
    else:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("摘要原文材料为空")
        context = "原文材料（尚未完成结构化分析；仅依据原文总结）：\n" + content
    limit = cfg.get("max_input_chars", 240000)
    if len(context) > limit:
        raise ValueError(
            f"摘要材料 {len(context)} 字符超过输入上限 {limit}，未截取部分正文"
        )
    profile = cfg["profiles"][depth]
    template = cfg["user_prompt_template"]
    if analysis is None:
        template = template.replace("已完成的分析：", "原文材料：")
    prompt = (
        template.format(title=title, context=context) + "\n\n" + profile["instruction"]
    )
    return SummaryRequest(
        messages=[
            {"role": "system", "content": cfg["system_prompt"] + "\n" + _grounding()},
            {"role": "user", "content": prompt},
        ],
        max_tokens=profile["max_tokens"],
        temperature=cfg.get("temperature", 0.1),
        policy_fingerprint=_policy_fingerprint(config),
        config=deepcopy(config),
    )


def normalize_summary(text: str) -> str:
    text = re.sub(r"\\\(\s*(.+?)\s*\\\)", r"$\1$", text)
    return re.sub(r"\\\[\s*(.+?)\s*\\\]", r"$$\1$$", text, flags=re.DOTALL)


def _validate_execution(
    request: SummaryRequest, llm, call_start: int, text: str
) -> str:
    if not text.strip():
        raise ValueError("分析总结为空，停止保存")
    if any(
        call.get("complete") is not True
        for call in getattr(llm, "calls", [])[call_start:]
    ):
        raise ValueError("摘要模型调用不完整，停止保存")
    if request.policy_fingerprint != _policy_fingerprint(request.config):
        raise ValueError("摘要期间模型或规则已更新，请重试")
    return normalize_summary(text)


async def generate_summary(request: SummaryRequest, llm_client) -> str:
    call_start = len(getattr(llm_client, "calls", []))
    text = await llm_client.chat(
        request.messages, temperature=request.temperature, max_tokens=request.max_tokens
    )
    return _validate_execution(request, llm_client, call_start, text)
