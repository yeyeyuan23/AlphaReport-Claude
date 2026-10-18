from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from reportagent.llm.client import LLMClient
from reportagent.llm.errors import ContextWindowExceeded
from reportagent.models.schemas import (
    AnalysisResult,
    Assessment,
    QualityJudgment,
    AShareApplicability,
    BiasRisks,
    DataInfo,
    FactorInfo,
    Methodology,
    ModelArchitecture,
    PortfolioConstruction,
    Reproducibility,
)
from reportagent.skills.registry import skill_resource
from reportagent.services.text_evidence import text_chunks

logger = logging.getLogger(__name__)


def _normalize_latex_delimiters(text: str) -> str:
    """Convert \\(...\\) to $...$ and \\[...\\] to $$...$$ for consistent rendering."""
    # Inline: \(...\) → $...$
    text = re.sub(r"\\\(\s*(.+?)\s*\\\)", r"$\1$", text)
    # Display: \[...\] → $$...$$
    text = re.sub(r"\\\[\s*(.+?)\s*\\\]", r"$$\1$$", text, flags=re.DOTALL)
    return text


class AnalysisAgent:
    def __init__(
        self,
        llm_client: LLMClient | None = None,
        config_path: str | None = None,
    ):
        self._llm = llm_client
        self._config = self._load_config(config_path)
        self._current_depth = "standard"

    def _load_config(self, config_path: str | None = None) -> dict:
        path = config_path or str(skill_resource("analyze-report", "analysis.yaml"))
        if Path(path).exists():
            with open(path, encoding="utf-8") as f:
                config = yaml.safe_load(f)
            grounding = skill_resource("analyze-report", "grounding.md").read_text(
                encoding="utf-8"
            )
            phases = config.get("analysis", {}).get("phases", {})
            if "assessment" in phases:
                shared = yaml.safe_load(
                    skill_resource("analyze-report", "assessment.yaml").read_text(
                        encoding="utf-8"
                    )
                )
                phases["assessment"].update(shared)
                phases["assessment"]["system_prompt"] += "\n" + skill_resource(
                    "analyze-report", "quality-rubric.md"
                ).read_text(encoding="utf-8")
            for phase in phases.values():
                phase["system_prompt"] = (
                    phase.get("system_prompt", "") + "\n" + grounding
                )
            config.setdefault("analysis", {})["summary"] = yaml.safe_load(
                skill_resource("analyze-report", "summary.yaml").read_text(
                    encoding="utf-8"
                )
            )
            return config
        return {}

    def _get_llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient()
        return self._llm

    def _phase_enabled(self, phase: str) -> bool:
        depth = self._current_depth
        if depth == "quick":
            return phase == "metadata"
        phases_cfg = self._config.get("analysis", {}).get("phases", {})
        return phases_cfg.get(phase, {}).get("enabled", True)

    def _phase_config(self, phase: str) -> dict:
        return self._config.get("analysis", {}).get("phases", {}).get(phase, {})

    def _phase_content(self, text: str) -> str:
        return "\n\n".join(
            f"[{key}]\n{value}" for key, value in text_chunks(text).items()
        )

    def _phase_model(self, cfg: dict) -> str | None:
        """Return model override if phase specifies a non-default model."""
        m = cfg.get("model", "default")
        return m if m != "default" else None

    async def analyze_report_directly(
        self,
        title: str,
        abstract: str,
        full_text: str,
        equations_json: str | None = None,
        depth: str = "standard",
        progress_cb=None,
    ) -> AnalysisResult:
        self._current_depth = depth
        return await self._analyze_single(
            title, abstract, full_text, equations_json, progress_cb=progress_cb
        )

    async def _analyze_single(
        self,
        title: str,
        abstract: str,
        full_text: str,
        equations_json: str | None = None,
        progress_cb=None,
    ) -> AnalysisResult:
        result = AnalysisResult(depth=self._current_depth)

        # Phase 0: Convert inline formulas to LaTeX
        latexify_cfg = self._config.get("analysis", {}).get("latexify", {})
        if (
            latexify_cfg.get("enabled", True)
            and full_text
            and self._current_depth != "quick"
        ):
            try:
                from reportagent.processors.math_latexifier import latexify_text

                full_text = await latexify_text(
                    full_text,
                    title=title,
                    llm_client=self._get_llm(),
                    max_chars_per_chunk=latexify_cfg.get("max_chars_per_chunk", 4000),
                    max_math_paragraphs=latexify_cfg.get("max_math_paragraphs", 30),
                )
            except Exception:
                logger.warning("Latexify step failed, using original text")
        if progress_cb:
            await progress_cb("metadata", "提取元数据...")

        # Phase 1: Metadata extraction
        if self._phase_enabled("metadata"):
            result = await self._run_phase_metadata(title, abstract, full_text, result)
        if progress_cb and self._phase_enabled("methodology"):
            await progress_cb("methodology", "解析方法论...")

        # Phase 2: Methodology deep dive with formula analysis
        if self._phase_enabled("methodology"):
            result = await self._run_phase_methodology(
                title, full_text, result, equations_json
            )
        if progress_cb and self._phase_enabled("assessment"):
            await progress_cb("assessment", "综合评估...")

        # Phase 4: Assessment
        if self._phase_enabled("assessment"):
            result = await self._run_phase_assessment(title, full_text, result)
        if progress_cb:
            await progress_cb("summary", "生成总结...")

        result.analyzed_at = datetime.now(timezone.utc).isoformat()
        result.summary = await self._synthesize_summary(result, title)

        # Post-process: convert any remaining plain math in output fields to LaTeX
        if latexify_cfg.get("enabled", True):
            result = await self._latexify_result(result, title)

        return result

    # ---- Phase 1: Metadata ----

    async def _run_phase_metadata(
        self, title: str, abstract: str, full_text: str, result: AnalysisResult
    ) -> AnalysisResult:
        cfg = self._phase_config("metadata")
        max_chars = len(full_text)
        content = self._phase_content(full_text)

        prompt = cfg.get("user_prompt_template", "").format(
            title=title,
            abstract=abstract,
            content=content,
            max_chars=max_chars,
        )
        schema = cfg.get("output_schema", "")
        if schema:
            prompt += f"\n\n请严格按照以下 JSON Schema 返回，只返回 JSON：\n{schema}"

        try:
            resp = await self._get_llm().chat_json(
                [
                    {"role": "system", "content": cfg.get("system_prompt", "")},
                    {"role": "user", "content": prompt},
                ],
                temperature=cfg.get("temperature", 0.1),
                max_tokens=cfg.get("max_tokens", 800),
                model=self._phase_model(cfg),
            )
            result.research_question = resp.get("research_question", "")
            result.core_contribution = resp.get("core_contribution", "")
            result.method_category = resp.get("method_category", "")
            result.benchmark_models = resp.get("benchmark_models", [])
            di = resp.get("data_used", {})
            result.data_used = DataInfo(
                market=di.get("market", ""),
                instruments=di.get("instruments", []),
                frequency=di.get("frequency", ""),
                sample_period=di.get("sample_period", ""),
                universe=di.get("universe", ""),
            )
        except ContextWindowExceeded:
            raise
        except Exception as e:
            logger.warning("Phase 1 (metadata) failed for '%s': %s", title[:50], e)

        return result

    # ---- Phase 2: Methodology ----

    async def _run_phase_methodology(
        self,
        title: str,
        full_text: str,
        result: AnalysisResult,
        equations_json: str | None = None,
    ) -> AnalysisResult:
        cfg = self._phase_config("methodology")
        max_chars = len(full_text)
        content = self._phase_content(full_text)

        # Parse extracted equations for inclusion in the prompt
        equations_text = ""
        if equations_json:
            try:
                eqs = json.loads(equations_json)
                if eqs:
                    eq_lines = []
                    for i, eq in enumerate(eqs[: cfg.get("max_context_equations", 20)]):
                        latex = eq.get("latex", "") if isinstance(eq, dict) else str(eq)
                        eq_lines.append(f"公式{i}：$${latex}$$")
                    if eq_lines:
                        equations_text = "\n".join(eq_lines)
            except (json.JSONDecodeError, TypeError):
                pass

        prompt = cfg.get("user_prompt_template", "").format(
            title=title,
            content=content,
            max_chars=max_chars,
            equations=equations_text,
        )
        schema = cfg.get("output_schema", "")
        if schema:
            prompt += f"\n\n请严格按照以下 JSON Schema 返回，只返回 JSON：\n{schema}"

        try:
            resp = await self._get_llm().chat_json(
                [
                    {"role": "system", "content": cfg.get("system_prompt", "")},
                    {"role": "user", "content": prompt},
                ],
                temperature=cfg.get("temperature", 0.2),
                max_tokens=cfg.get("max_tokens", 2500),
                model=self._phase_model(cfg),
            )

            factors = [
                FactorInfo(
                    name=f.get("name", ""),
                    type=f.get("type", ""),
                    construction=f.get("construction", ""),
                    raw_or_neutralized=f.get("raw_or_neutralized", ""),
                    formula_index=f.get("formula_index"),
                )
                for f in resp.get("factor_list", [])
            ]

            ma = resp.get("model_architecture")
            model_arch = None
            if ma:
                model_arch = ModelArchitecture(
                    type=ma.get("type", ""),
                    layers_or_structure=ma.get("layers_or_structure", ""),
                    loss_function=ma.get("loss_function", ""),
                    regularization=ma.get("regularization", ""),
                    training_scheme=ma.get("training_scheme", ""),
                )

            pc = resp.get("portfolio_construction")
            port_con = None
            if pc:
                port_con = PortfolioConstruction(
                    weighting=pc.get("weighting", ""),
                    rebalance_frequency=pc.get("rebalance_frequency", ""),
                    constraints=pc.get("constraints", ""),
                    transaction_cost_model=pc.get("transaction_cost_model", ""),
                )

            result.methodology = Methodology(
                analysis_points=resp.get("analysis_points", []),
                factor_list=factors,
                model_architecture=model_arch,
                portfolio_construction=port_con,
            )
        except ContextWindowExceeded:
            raise
        except Exception as e:
            logger.warning("Phase 2 (methodology) failed for '%s': %s", title[:50], e)

        return result

    # ---- Phase 4: Assessment ----

    async def _run_phase_assessment(
        self, title: str, full_text: str, result: AnalysisResult
    ) -> AnalysisResult:
        cfg = self._phase_config("assessment")
        max_chars = len(full_text)
        content = self._phase_content(full_text)

        extra = ""
        if result.equations:
            extra = f"\n该论文包含 {len(result.equations)} 个提取出的公式。"

        prompt = cfg.get("user_prompt_template", "").format(
            title=title,
            content=content,
            max_chars=max_chars,
            research_question=result.research_question or "未知",
            core_contribution=result.core_contribution or "未知",
            method_category=result.method_category or "未知",
            benchmarks=", ".join(result.benchmark_models)
            if result.benchmark_models
            else "未知",
            extra_context=extra,
        )
        schema = cfg.get("output_schema", "")
        if schema:
            prompt += f"\n\n请严格按照以下 JSON Schema 返回，只返回 JSON：\n{schema}"

        try:
            resp = await self._get_llm().chat_json(
                [
                    {"role": "system", "content": cfg.get("system_prompt", "")},
                    {"role": "user", "content": prompt},
                ],
                temperature=cfg.get("temperature", 0.3),
                max_tokens=cfg.get("max_tokens", 2000),
                model=self._phase_model(cfg),
            )

            br = resp.get("bias_risks", {})
            bias_risks = BiasRisks(
                look_ahead_bias=br.get("look_ahead_bias", "unknown"),
                survivorship_bias=br.get("survivorship_bias", "unknown"),
                data_snooping=br.get("data_snooping", "unknown"),
                overfitting_concern=br.get("overfitting_concern", ""),
            )

            rp = resp.get("reproducibility", {})
            reproducibility = Reproducibility(
                level=rp.get("level", "unknown"),
                has_data_source=rp.get("has_data_source"),
                has_code_available=rp.get("has_code_available"),
                missing_details=rp.get("missing_details", []),
            )

            sa = resp.get("a_share_applicability")
            a_share = None
            if sa:
                a_share = AShareApplicability(
                    directly_applicable=sa.get("directly_applicable"),
                    adaptations_needed=sa.get("adaptations_needed", []),
                    key_constraints=sa.get("key_constraints", []),
                )

            result.assessment = Assessment(
                **QualityJudgment.model_validate(resp).model_dump(),
                strengths=resp.get("strengths", []),
                weaknesses=resp.get("weaknesses", []),
                bias_risks=bias_risks,
                reproducibility=reproducibility,
                a_share_applicability=a_share,
                marginal_contribution_summary=resp.get(
                    "marginal_contribution_summary", ""
                ),
                practical_implications=resp.get("practical_implications", []),
                key_contributions=resp.get("key_contributions", []),
            )
        except ContextWindowExceeded:
            raise
        except Exception as e:
            logger.warning("Phase 4 (assessment) failed for '%s': %s", title[:50], e)

        return result

    # ---- Output latexify ----

    async def _latexify_result(
        self, result: AnalysisResult, title: str
    ) -> AnalysisResult:
        """Post-process: normalize LaTeX delimiters and convert any remaining
        plain-math text in output fields via LLM."""
        from reportagent.processors.math_latexifier import latexify_text

        # Phase A: fast regex normalization on all text fields
        fields = self._collect_result_fields(result)
        for field_name, text in fields:
            normalized = _normalize_latex_delimiters(text)
            if normalized != text:
                self._set_result_field(result, field_name, normalized)

        # Phase B: LLM latexify on summary (always, to inject LaTeX into plain-text fields)
        if result.summary:
            try:
                result.summary = _normalize_latex_delimiters(
                    await latexify_text(
                        result.summary, title=title, llm_client=self._get_llm()
                    )
                )
            except Exception:
                pass

        return result

    def _collect_result_fields(self, result: AnalysisResult) -> list[tuple[str, str]]:
        """Collect all text fields from the result for normalization."""
        fields: list[tuple[str, str]] = [
            ("research_question", result.research_question),
            ("core_contribution", result.core_contribution),
            ("summary", result.summary),
        ]
        if result.assessment:
            for i, s in enumerate(result.assessment.strengths):
                fields.append((f"assessment.strengths.{i}", s))
            for i, w in enumerate(result.assessment.weaknesses):
                fields.append((f"assessment.weaknesses.{i}", w))
            for i, c in enumerate(result.assessment.key_contributions):
                fields.append((f"assessment.key_contributions.{i}", c))
            if result.assessment.marginal_contribution_summary:
                fields.append(
                    (
                        "assessment.marginal_contribution_summary",
                        result.assessment.marginal_contribution_summary,
                    )
                )
            for i, imp in enumerate(result.assessment.practical_implications):
                fields.append((f"assessment.practical_implications.{i}", imp))
        if result.methodology:
            for i, point in enumerate(result.methodology.analysis_points):
                if isinstance(point, dict):
                    if point.get("analysis"):
                        fields.append(
                            (
                                f"methodology.analysis_points.{i}.analysis",
                                point["analysis"],
                            )
                        )
                    if point.get("marginal_contribution"):
                        fields.append(
                            (
                                f"methodology.analysis_points.{i}.marginal_contribution",
                                point["marginal_contribution"],
                            )
                        )
                    if point.get("practical_implication"):
                        fields.append(
                            (
                                f"methodology.analysis_points.{i}.practical_implication",
                                point["practical_implication"],
                            )
                        )
            for i, f in enumerate(result.methodology.factor_list):
                if f.construction:
                    fields.append(
                        (f"methodology.factor_list.{i}.construction", f.construction)
                    )
        # Phase 3 equation explanations — often contain \(...\) from LLM output
        for i, eq in enumerate(result.equations):
            if eq.meaning:
                fields.append((f"equations.{i}.meaning", eq.meaning))
            if eq.role_in_paper:
                fields.append((f"equations.{i}.role_in_paper", eq.role_in_paper))
        return [(k, v) for k, v in fields if v]

    @staticmethod
    def _set_result_field(result: AnalysisResult, field_name: str, value: str):
        """Set a nested field on AnalysisResult by dotted path."""
        parts = field_name.split(".")
        obj: Any = result
        for part in parts[:-1]:
            if part.isdigit():
                obj = obj[int(part)]
            elif hasattr(obj, part):
                obj = getattr(obj, part)
            else:
                return
        last = parts[-1]
        if last.isdigit():
            obj[int(last)] = value
        elif hasattr(obj, last):
            setattr(obj, last, value)

    # ---- Summary synthesis ----

    async def _synthesize_summary(self, result: AnalysisResult, title: str) -> str:
        from reportagent.services.summary import build_summary_request, generate_summary

        request = build_summary_request(
            title,
            analysis=result,
            depth=self._current_depth,
            config=self._config["analysis"]["summary"],
        )
        return await generate_summary(request, self._get_llm())
