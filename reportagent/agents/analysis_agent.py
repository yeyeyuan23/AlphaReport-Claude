from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from reportagent.agents.state import AgentState
from reportagent.llm.client import LLMClient
from reportagent.models.schemas import (
    AnalysisResult,
    Assessment,
    AShareApplicability,
    BiasRisks,
    DataInfo,
    EquationExplanation,
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


def _equation_explanation_fields(explanation: dict) -> dict:
    """Keep only well-typed explanation fields, including from older saved output."""
    fields = {}
    for name in ("meaning", "role_in_paper"):
        if isinstance(explanation.get(name), str):
            fields[name] = explanation[name]
    symbols = explanation.get("symbols")
    if isinstance(symbols, dict) and all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in symbols.items()
    ):
        fields["symbols"] = symbols
    if isinstance(explanation.get("is_key_formula"), bool):
        fields["is_key_formula"] = explanation["is_key_formula"]
    return fields


def _merge_equation_explanations(
    batch: list[dict], response: Any, batch_start: int
) -> list[dict]:
    """Merge valid explanations by global index without changing source evidence."""
    enriched = [
        dict(eq) if isinstance(eq, dict) else {"latex": str(eq)} for eq in batch
    ]
    if not isinstance(response, dict) or not isinstance(
        response.get("equations"), list
    ):
        return enriched

    seen: set[int] = set()
    for explanation in response["equations"]:
        if not isinstance(explanation, dict):
            continue
        index = explanation.get("index")
        if (
            type(index) is not int
            or not batch_start <= index < batch_start + len(batch)
            or index in seen
        ):
            continue

        # Model output may enrich a source formula, never replace its LaTeX or page.
        fields = _equation_explanation_fields(explanation)
        if fields:
            # Keep the first valid reply for an index; never duplicate its source.
            enriched[index - batch_start].update(fields)
            seen.add(index)

    return enriched


async def explain_equations(
    equations: list[dict],
    title: str,
    context_text: str,
    llm_client: LLMClient | None = None,
    config: dict | None = None,
) -> list[dict]:
    """Shared utility: enrich equations with LLM-powered explanations.

    Takes MinerU-extracted equations (latex + page_idx) and returns them
    enriched with meaning, symbol definitions, and role-in-paper.
    Used by the /parse endpoint; shares source-preserving merging with Phase 3.
    """
    if not equations:
        return []

    if llm_client is None:
        llm_client = LLMClient()

    if config is None:
        config = {}
    eq_cfg = config.get("analysis", {}).get("phases", {}).get("equations", {})
    batch_size = eq_cfg.get("max_formulas_per_batch", 3)

    enriched: list[dict] = []

    for batch_start in range(0, len(equations), batch_size):
        batch = equations[batch_start : batch_start + batch_size]
        eq_lines = []
        for i, eq in enumerate(batch):
            idx = batch_start + i
            latex = eq.get("latex", "") if isinstance(eq, dict) else str(eq)
            eq_lines.append(f"公式 {idx}: {latex}")

        prompt = (
            "以下是一篇量化金融论文中的数学公式，请逐一解读每个公式：\n\n"
            f"论文标题：{title}\n\n"
            f"论文上下文（部分）：\n{context_text[:3000]}\n\n"
            "公式列表：\n" + "\n".join(eq_lines) + "\n\n"
            "index 必须使用公式列表中的原始编号，不要按本批次从 0 重新编号。\n"
            f"请对这 {len(batch)} 个公式逐一解读，所有文本用中文输出，返回 JSON：\n"
            f'{{"equations": [{{"index": {batch_start}, "meaning": "公式含义（一句话）", '
            '"symbols": {"符号": "含义", ...}, '
            '"role_in_paper": "在论文中的作用", "is_key_formula": true/false}]}'
        )

        resp = None
        try:
            resp = await llm_client.chat_json(
                [
                    {
                        "role": "system",
                        "content": "你是量化金融数学专家，擅长解读公式。所有输出必须用中文，包括公式含义、符号说明、作用描述。",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=1500,
            )
        except Exception:
            logger.warning("Equation batch %d failed for '%s'", batch_start, title[:50])
        enriched.extend(_merge_equation_explanations(batch, resp, batch_start))

    return enriched


class AnalysisAgent:
    def __init__(
        self,
        llm_client: LLMClient | None = None,
        config_path: str | None = None,
    ):
        self._llm = llm_client
        self._config = self._load_config(config_path)
        self.progress_cb = None
        self.cancel_check = None
        self._current_depth = "standard"

    def _load_config(self, config_path: str | None = None) -> dict:
        path = config_path or str(skill_resource("analyze-report", "analysis.yaml"))
        if Path(path).exists():
            with open(path, encoding="utf-8") as f:
                config = yaml.safe_load(f)
            grounding = skill_resource("analyze-report", "grounding.md").read_text(
                encoding="utf-8"
            )
            for phase in config.get("analysis", {}).get("phases", {}).values():
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

    def _phase_content(self, text: str, cfg: dict) -> str:
        limit = cfg.get("max_input_chars", 240000)
        if len(text) > limit:
            raise ValueError(
                f"全文 {len(text)} 字符超过阶段输入上限 {limit}，停止分析；未截取部分正文"
            )
        return "\n\n".join(
            f"[{key}]\n{value}" for key, value in text_chunks(text).items()
        )

    def _phase_model(self, cfg: dict) -> str | None:
        """Return model override if phase specifies a non-default model."""
        m = cfg.get("model", "default")
        return m if m != "default" else None

    async def run(self, state: AgentState) -> AgentState:
        self._current_depth = self._config.get("analysis", {}).get("depth", "standard")
        reports = state.get("classified_reports", [])
        total = len(reports)

        state["analysis_status"] = "analyzing"
        state["current_phase"] = "analyzing"

        msg = f"Analyzing {total} reports (depth: {self._current_depth})..."
        state["messages"].append(msg)
        if self.progress_cb:
            self.progress_cb("analyzing", msg)

        for i, cr in enumerate(reports):
            if self.cancel_check and self.cancel_check():
                state["analysis_status"] = "cancelled"
                state["messages"].append("Analysis cancelled by user")
                return state

            sr = cr.search_result
            title = sr.title or ""
            full_text = sr.full_text or sr.abstract or ""

            if not full_text.strip():
                continue

            try:
                result = await self._analyze_single(
                    title, sr.abstract or "", full_text, sr.equations_json
                )
            except Exception as e:
                logger.warning("Analysis failed for '%s': %s", title[:50], e)
                result = AnalysisResult(summary=f"Analysis failed: {e}")

            cr.analysis = result

            progress_msg = f"Analyzed {i + 1}/{total}: {title[:40]}..."
            state["messages"].append(progress_msg)
            if self.progress_cb:
                self.progress_cb("analyzing", progress_msg)

        state["analysis_status"] = "done"
        state["messages"].append(f"Analysis complete: {total} reports")
        return state

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
        if progress_cb:
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
        content = self._phase_content(full_text, cfg)

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
        content = self._phase_content(full_text, cfg)

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
        except Exception as e:
            logger.warning("Phase 2 (methodology) failed for '%s': %s", title[:50], e)

        return result

    # ---- Phase 3: Equations ----

    async def _run_phase_equations(
        self, title: str, full_text: str, equations_json: str, result: AnalysisResult
    ) -> AnalysisResult:
        try:
            equations = json.loads(equations_json)
        except (json.JSONDecodeError, TypeError):
            return result

        if not equations:
            return result

        # Normalize PDF extraction artifacts in formulas
        from reportagent.processors.formula_normalizer import normalize_formulas

        # Missing/non-text extraction is unknown, not a formula to stringify.
        equations = [
            {
                **eq,
                "latex": eq["latex"] if isinstance(eq.get("latex"), str) else "",
            }
            if isinstance(eq, dict)
            else eq
            for eq in equations
        ]
        equations = normalize_formulas(equations)

        cfg = self._phase_config("equations")
        batch_size = cfg.get("max_formulas_per_batch", 3)
        eq_context_chars = cfg.get("max_context_chars", 8000)
        max_equations = cfg.get("max_equations", 15)
        all_explanations: list[EquationExplanation] = []

        equations_to_process = equations[:max_equations]

        for batch_start in range(0, len(equations_to_process), batch_size):
            batch = equations_to_process[batch_start : batch_start + batch_size]
            eq_list = []
            for i, eq in enumerate(batch):
                idx = batch_start + i
                latex = eq.get("latex", "") if isinstance(eq, dict) else str(eq)
                eq_list.append(f"公式 {idx}: {latex}")

            prompt = cfg.get("user_prompt_template", "").format(
                title=title,
                context=full_text[:eq_context_chars],
                equations="\n".join(eq_list),
                count=len(batch),
            )
            prompt += (
                "\nindex 必须使用公式列表中的原始编号，不要按本批次从 0 重新编号。"
            )
            schema = cfg.get("output_schema", "")
            if schema:
                prompt += (
                    f"\n\n请严格按照以下 JSON Schema 返回，只返回 JSON：\n{schema}"
                )

            resp = None
            try:
                resp = await self._get_llm().chat_json(
                    [
                        {"role": "system", "content": cfg.get("system_prompt", "")},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=cfg.get("temperature", 0.1),
                    max_tokens=cfg.get("max_tokens", 1500),
                    model=self._phase_model(cfg),
                )
            except Exception:
                logger.warning(
                    "Phase 3 batch %d failed for '%s'", batch_start, title[:50]
                )
            for offset, eq in enumerate(
                _merge_equation_explanations(batch, resp, batch_start)
            ):
                all_explanations.append(
                    EquationExplanation(
                        index=batch_start + offset,
                        latex=eq.get("latex", ""),
                        **_equation_explanation_fields(eq),
                    )
                )

        result.equations = all_explanations
        return result

    # ---- Phase 4: Assessment ----

    async def _run_phase_assessment(
        self, title: str, full_text: str, result: AnalysisResult
    ) -> AnalysisResult:
        cfg = self._phase_config("assessment")
        max_chars = len(full_text)
        content = self._phase_content(full_text, cfg)

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
                has_data_source=rp.get("has_data_source", False),
                has_code_available=rp.get("has_code_available", False),
                missing_details=rp.get("missing_details", []),
            )

            sa = resp.get("a_share_applicability")
            a_share = None
            if sa:
                a_share = AShareApplicability(
                    directly_applicable=sa.get("directly_applicable", False),
                    adaptations_needed=sa.get("adaptations_needed", []),
                    key_constraints=sa.get("key_constraints", []),
                )

            result.assessment = Assessment(
                overall_quality_score=resp.get("overall_quality_score", 0.0),
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
        except Exception as e:
            logger.warning("Phase 4 (assessment) failed for '%s': %s", title[:50], e)

        return result

    # ---- Merge equations into methodology ----

    async def _merge_equations_into_methodology(
        self, result: AnalysisResult, title: str
    ) -> None:
        """Merge formula explanations into methodology step descriptions.

        Rewrites each step's description to naturally embed related formula
        explanations, so formulas are not listed separately but explained in
        the context of the methodology they belong to.
        """
        if not result.methodology or not result.methodology.analysis_points:
            return
        if not result.equations:
            return

        steps = result.methodology.analysis_points
        equations = result.equations

        # Collect all points that have related formulas, batch into one LLM call
        merge_tasks: list[dict] = []
        for i, point in enumerate(steps):
            if not isinstance(point, dict):
                continue
            related = point.get("related_formulas", [])
            if not related:
                continue

            eq_context_parts = []
            for fi in related:
                if fi < len(equations):
                    eq = equations[fi]
                    eq_context_parts.append(
                        f"公式{fi}：$${eq.latex}$$\n"
                        f"含义：{eq.meaning}\n"
                        f"在论文中的作用：{eq.role_in_paper}"
                    )

            if eq_context_parts:
                merge_tasks.append(
                    {
                        "step_idx": i,
                        "title": point.get("title", ""),
                        "analysis": point.get("analysis", ""),
                        "formulas": "\n\n".join(eq_context_parts),
                    }
                )

        if not merge_tasks:
            return

        # Build one batched prompt for all steps
        sections = []
        for i, task in enumerate(merge_tasks):
            sections.append(
                f"### 分析点{i + 1}：{task['title']}\n"
                f"原分析：{task['analysis']}\n"
                f"关联公式解读：\n{task['formulas']}"
            )
        batched = "\n\n---\n\n".join(sections)

        prompt = (
            "你是一位量化金融方法论专家。以下是论文方法步骤及其关联公式的解读。\n"
            "请将每个步骤的公式解读**融入**到步骤描述中，形成连贯的叙述段落。\n\n"
            "【强制格式要求 - 必须严格遵守】\n"
            "- 完整公式用 $$...$$ 单独一行展示，变量符号用 $...$ 行内包裹。\n"
            "- 严禁使用 \\(...\\) 或 \\[...\\] 格式。\n"
            "- 每个步骤的描述应为 3-6 句的连贯叙述，公式作为描述的一部分自然呈现。\n"
            "- 不要单独列出公式，公式及其含义应融入方法描述中。\n\n"
            "【输出格式】用 JSON 返回，key 为步骤编号（step_idx），value 为融合后的描述文本：\n"
            '{"descriptions": {"1": "融合后描述...", "2": "融合后描述..."}}\n\n'
            f"论文标题：{title}\n\n"
            f"{batched}"
        )

        try:
            resp = await self._get_llm().chat_json(
                [
                    {
                        "role": "system",
                        "content": "你是量化金融方法论专家。所有输出用中文。严格按照 JSON 格式返回。",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=3000,
            )
            desc_map = resp.get("descriptions", {})
            for task in merge_tasks:
                new_desc = desc_map.get(str(task["step_idx"]), "")
                if new_desc:
                    idx = task["step_idx"]
                    if idx < len(steps) and isinstance(steps[idx], dict):
                        steps[idx]["analysis"] = _normalize_latex_delimiters(new_desc)
        except Exception as e:
            logger.warning("Merge equations into methodology failed: %s", e)

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
        """Generate a flowing narrative prose summary via LLM, with formulas inline."""
        context_parts: list[str] = []
        if result.research_question:
            context_parts.append(f"研究问题：{result.research_question}")
        if result.core_contribution:
            context_parts.append(f"核心贡献：{result.core_contribution}")
        if result.data_used.market or result.data_used.sample_period:
            di = result.data_used
            context_parts.append(
                f"数据：{di.market}市场，{', '.join(di.instruments) if di.instruments else '—'}，"
                f"频率{di.frequency}，样本期{di.sample_period}，股票池{di.universe}"
            )

        if result.methodology:
            for point in result.methodology.analysis_points:
                if isinstance(point, dict):
                    context_parts.append(
                        f"洞察-{point.get('title', '')}：{point.get('analysis', '')}"
                    )
                    if point.get("marginal_contribution"):
                        context_parts.append(
                            f"  边际贡献：{point['marginal_contribution']}"
                        )
                    if point.get("practical_implication"):
                        context_parts.append(
                            f"  实践推论：{point['practical_implication']}"
                        )
            for f in result.methodology.factor_list:
                context_parts.append(f"因子-{f.name}（{f.type}）：{f.construction}")

        if result.equations:
            for eq in result.equations:
                if eq.is_key_formula:
                    context_parts.append(f"关键公式：${eq.latex}$ — {eq.meaning}")

        if result.assessment:
            a = result.assessment
            context_parts.append(f"质量评分：{a.overall_quality_score}")
            context_parts.append(f"优势：{'；'.join(a.strengths)}")
            context_parts.append(f"不足：{'；'.join(a.weaknesses)}")
            if a.marginal_contribution_summary:
                context_parts.append(f"边际贡献总评：{a.marginal_contribution_summary}")
            if a.practical_implications:
                context_parts.append(f"实践推论：{'；'.join(a.practical_implications)}")

        context = "\n\n".join(context_parts)

        cfg = self._config["analysis"]["summary"]
        profile = cfg["profiles"][self._current_depth]
        grounding = skill_resource("analyze-report", "grounding.md").read_text(
            encoding="utf-8"
        )
        prompt = cfg["user_prompt_template"].format(title=title, context=context)
        prompt += "\n\n" + profile["instruction"]
        summary = await self._get_llm().chat(
            [
                {"role": "system", "content": cfg["system_prompt"] + "\n" + grounding},
                {"role": "user", "content": prompt},
            ],
            temperature=cfg.get("temperature", 0.1),
            max_tokens=profile["max_tokens"],
        )
        if not summary.strip():
            raise ValueError("分析总结为空，停止保存")
        return _normalize_latex_delimiters(summary)
