"""Generate structured narrative knowledge cards from stored analysis."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging


from reportagent.db.engine import init_db, get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.models.schemas import ReportListParams, KnowledgeCard
from reportagent.llm.client import LLMClient
from reportagent.skills.registry import skill_resource

logger = logging.getLogger("narrative_brief")


@dataclass(frozen=True)
class NarrativeOptions:
    report_id: int | None = None
    recent: int | None = None
    force: bool = False
    dry_run: bool = False


def build_analysis_context(analysis_json: str, title: str, abstract: str | None) -> str:
    """从 analysis_json 中提取关键信息拼接为上下文。"""
    try:
        data = json.loads(analysis_json)
    except (json.JSONDecodeError, TypeError):
        return f"摘要：{abstract or '无'}"

    parts = []

    # Research question & core contribution
    if data.get("research_question"):
        parts.append(f"研究问题：{data['research_question']}")
    if data.get("core_contribution"):
        parts.append(f"核心贡献：{data['core_contribution']}")

    # Data info
    di = data.get("data_used", {})
    if di:
        parts.append(
            f"数据：{di.get('market', '')}，工具：{di.get('instruments', [])}，"
            f"频率：{di.get('frequency', '')}，样本期：{di.get('sample_period', '')}，"
            f"股票池：{di.get('universe', '')}"
        )

    # Methodology
    method = data.get("methodology", {})
    if method:
        for point in method.get("analysis_points", []):
            if isinstance(point, dict):
                parts.append(f"[方法] {point.get('title', '')}：{point.get('analysis', '')}")
                if point.get("marginal_contribution"):
                    parts.append(f"  边际贡献：{point['marginal_contribution']}")
                if point.get("practical_implication"):
                    parts.append(f"  实践推论：{point['practical_implication']}")
        for f in method.get("factor_list", []):
            parts.append(f"[因子] {f.get('name', '')}（{f.get('type', '')}）：{f.get('construction', '')}")

    # Key equations
    for eq in data.get("equations", []):
        if eq.get("is_key_formula"):
            parts.append(f"[关键公式] ${eq.get('latex', '')}$ — {eq.get('meaning', '')}")

    # Assessment
    assessment = data.get("assessment", {})
    if assessment:
        parts.append(
            f"质量评分：{assessment.get('overall_quality_score', 'N/A')}"
        )
        parts.append(f"优势：{'；'.join(assessment.get('strengths', []))}")
        parts.append(f"不足：{'；'.join(assessment.get('weaknesses', []))}")
        if assessment.get("marginal_contribution_summary"):
            parts.append(f"边际贡献总评：{assessment['marginal_contribution_summary']}")
        if assessment.get("practical_implications"):
            parts.append(f"实践推论：{'；'.join(assessment['practical_implications'])}")

    # Existing summary
    if data.get("summary"):
        parts.append(f"[AI 分析总结]\n{data['summary']}")

    return "\n\n".join(parts)


def _robust_json_parse(text: str) -> dict:
    """Parse JSON from LLM output with aggressive fixing for LaTeX and formatting issues.

    Handles: markdown fences, unescaped backslashes, unescaped newlines in strings,
    trailing commas, and extra text after the JSON block.
    """
    import re as _re

    cleaned = text.strip()
    # Strip markdown fences
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    # Try direct parse first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fix 1: Escape backslashes that are part of LaTeX (not valid JSON escapes)
    # Valid JSON escapes: \\, \", \/, \b, \f, \n, \r, \t, \uXXXX
    chars: list[str] = []
    i = 0
    while i < len(cleaned):
        c = cleaned[i]
        if c == "\\" and i + 1 < len(cleaned):
            nxt = cleaned[i + 1]
            if nxt in '"\\/bfnrtu':
                chars.append("\\")
                chars.append(nxt)
                i += 2
            elif nxt == "u" and i + 5 < len(cleaned) and all(
                h in "0123456789abcdefABCDEF" for h in cleaned[i + 2 : i + 6]
            ):
                chars.append("\\")
                chars.append(cleaned[i + 1 : i + 6])
                i += 6
            else:
                chars.append("\\\\")
                i += 1
        else:
            chars.append(c)
            i += 1

    try:
        return json.loads("".join(chars))
    except json.JSONDecodeError:
        pass

    # Fix 2: Try to extract the JSON object by finding outermost braces
    text2 = "".join(chars)
    brace_start = text2.find("{")
    if brace_start >= 0:
        # Count braces to find matching end
        depth = 0
        for j in range(brace_start, len(text2)):
            if text2[j] == "{":
                depth += 1
            elif text2[j] == "}":
                depth -= 1
                if depth == 0:
                    text2 = text2[brace_start : j + 1]
                    break

    # Fix 3: Remove control characters inside strings
    text2 = _re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', text2)

    # Fix 4: Try to fix unescaped newlines within string values
    # Find quoted strings and escape internal newlines
    def _fix_string_newlines(m: _re.Match) -> str:
        content = m.group(0)
        # Replace literal newlines inside the string with \\n
        return content.replace('\n', '\\n')

    # Only apply to strings that span multiple lines
    text2 = _re.sub(r'"(?:[^"\\]|\\.)*"', _fix_string_newlines, text2, flags=_re.DOTALL)

    try:
        return json.loads(text2)
    except json.JSONDecodeError:
        pass

    # Fix 5: As a last resort, try to extract fields via regex
    result: dict = {}
    patterns = {
        "summary": r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"',
        "highlights": r'"highlights"\s*:\s*\[(.*?)\]',
        "methodology_steps": r'"methodology_steps"\s*:\s*\[(.*?)\]',
        "results": r'"results"\s*:\s*"((?:[^"\\]|\\.)*)"',
        "marginal_contributions": r'"marginal_contributions"\s*:\s*"((?:[^"\\]|\\.)*)"',
        "implications": r'"implications"\s*:\s*\[(.*?)\]',
        "topics": r'"topics"\s*:\s*\[(.*?)\]',
        "asset_classes": r'"asset_classes"\s*:\s*\[(.*?)\]',
        "quality_score": r'"quality_score"\s*:\s*([\d.]+)',
    }
    for key, pat in patterns.items():
        m = _re.search(pat, text2, _re.DOTALL)
        if m:
            if key in ("highlights", "methodology_steps", "implications", "topics", "asset_classes"):
                # Extract array strings
                items = _re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
                result[key] = [it.replace('\\\\', '\\') for it in items]
            elif key == "quality_score":
                try:
                    result[key] = float(m.group(1))
                except ValueError:
                    pass
            else:
                result[key] = m.group(1).replace('\\\\', '\\')

    if result:
        result.setdefault("highlights", [])
        result.setdefault("methodology_steps", [])
        result.setdefault("implications", [])
        result.setdefault("topics", [])
        result.setdefault("asset_classes", [])
        result.setdefault("results", "")
        result.setdefault("marginal_contributions", "")
        logger.info("  Recovered via regex fallback (fix 5)")
        return result

    raise json.JSONDecodeError("All parsing strategies failed", text2, 0)


async def generate_knowledge_card(
    llm: LLMClient,
    title: str,
    abstract: str | None,
    analysis_json: str | None,
    topics_str: str | None,
    asset_classes_str: str | None,
) -> KnowledgeCard:
    """Generate a KnowledgeCard from analysis results."""

    if not analysis_json:
        # No analysis — generate minimal card from abstract
        return KnowledgeCard(
            summary=abstract or "暂无摘要",
            highlights=[],
            methodology_steps=[],
            results="",
            marginal_contributions="",
            implications=[],
            topics=topics_str.split(",") if topics_str else [],
            asset_classes=asset_classes_str.split(",") if asset_classes_str else [],
            quality_score=None,
        )

    context = build_analysis_context(analysis_json, title, abstract)
    prompt = skill_resource("analyze-report", "narrative-user.md").read_text(encoding="utf-8").format(
        title=title,
        analysis_context=context[:8000],
    )

    # Strategy: use plain chat (not chat_json) and do our own robust parsing,
    # because LaTeX-heavy content frequently breaks JSON parsers
    try:
        raw_text = await llm.chat(
            [
                {"role": "system", "content": skill_resource("analyze-report", "narrative-system.md").read_text(encoding="utf-8")},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=2500,
        )
        resp = _robust_json_parse(raw_text)
    except Exception as e:
        logger.warning("Knowledge card generation failed for '%s': %s", title[:50], e)
        return KnowledgeCard(
            summary=f"生成失败: {e}",
            highlights=[],
            methodology_steps=[],
            results="",
            marginal_contributions="",
            implications=[],
            topics=[],
            asset_classes=[],
            quality_score=None,
        )

    return KnowledgeCard(
        summary=resp.get("summary", ""),
        highlights=resp.get("highlights", []),
        methodology_steps=resp.get("methodology_steps", []),
        results=resp.get("results", ""),
        marginal_contributions=resp.get("marginal_contributions", ""),
        implications=resp.get("implications", []),
        topics=resp.get("topics", []),
        asset_classes=resp.get("asset_classes", []),
        quality_score=resp.get("quality_score"),
    )


async def run_narrative_brief(options: NarrativeOptions) -> None:
    init_db()
    factory = get_session_factory()
    session = factory()

    try:
        repo = ReportRepository(session)

        # Select reports
        if options.report_id:
            report = repo.get_report(options.report_id)
            if not report:
                logger.error(f"Report {options.report_id} not found")
                raise ValueError("Report not found")
            reports = [report]
        elif options.recent:
            params = ReportListParams(sort_by="created_at", limit=options.recent)
            reports, _ = repo.list_reports(params)
        else:
            params = ReportListParams(limit=200)
            reports, _ = repo.list_reports(params)

        # Filter: skip if already has knowledge_card and not forcing
        if not options.force:
            to_process = [r for r in reports
                          if not r.knowledge_card_json or not r.knowledge_card_json.strip()]
        else:
            to_process = list(reports)

        logger.info(f"Total reports: {len(reports)}, to process: {len(to_process)}")

        if not to_process:
            logger.info("All reports already have knowledge cards. Use --force to regenerate.")
            return

        llm = LLMClient()
        generated = 0
        errors = 0

        for i, r in enumerate(to_process):
            logger.info(f"[{i+1}/{len(to_process)}] Generating: {r.title[:80]}...")

            card = await generate_knowledge_card(
                llm,
                title=r.title,
                abstract=r.abstract,
                analysis_json=r.analysis_json,
                topics_str=r.topics,
                asset_classes_str=r.asset_classes,
            )

            card_json = card.model_dump_json(indent=2, exclude_none=False)

            if options.dry_run:
                logger.info(f"  [DRY-RUN] Card: {card.summary[:100]}...")
            else:
                r.knowledge_card_json = card_json
                repo.session.commit()
                logger.info(f"  ✅ Saved ({len(card_json)} bytes)")

            generated += 1

        if not options.dry_run:
            logger.info(f"\nDone: {generated} knowledge cards generated, {errors} errors")

    finally:
        session.close()
