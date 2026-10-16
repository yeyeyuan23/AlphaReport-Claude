"""Detect inline formulas in text and convert them to LaTeX via LLM.

MinerU and Docling both output formulas in plain Unicode (e.g. Greek letters,
subscript numbers). This module uses the LLM to rewrite paragraphs so that
every inline formula is wrapped in $...$ with proper LaTeX markup.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Fast heuristic: detect paragraphs that likely contain math notation
_MATH_INDICATORS = re.compile(
    r"[Α-ω℀-⅏∀-⋿■-⟿]|"  # Greek, math symbols, arrows
    r"\\[_a-zA-Z]|"  # LaTeX commands like \_ \alpha \beta \sum
    r"[a-zA-Z]_\{?[a-zA-Z0-9]+\}?|"  # Subscripts: r_{i,t}, holding_ret_adj
    r"[a-zA-Z]\^\{?[a-zA-Z0-9]+\}?|"  # Superscripts
    r"\\frac\{|"  # Fractions
    r"\bICIR\b|\bIC\b|"  # Quant finance metrics
    r"\d+\.?\d*%\s*(?:IC|IR|收益)|"  # Metric descriptions with percentages
    r"(?:holding|ret|price|amt|turnover)_[a-z]|"  # Common quant variable naming
    r"[＝=]\s*(?:-?\d+\.?\d*|\\?[a-zA-Z])"  # Assignment/equation pattern
)


def has_math(text: str) -> bool:
    """Quick check if text likely contains mathematical notation."""
    return bool(_MATH_INDICATORS.search(text))


async def latexify_text(
    text: str,
    title: str = "",
    llm_client=None,
    max_chars_per_chunk: int = 4000,
    max_math_paragraphs: int = 30,
) -> str:
    """Convert inline math to LaTeX $...$ throughout the entire text.

    Paragraphs are batched and sent to the LLM for conversion. Non-math
    paragraphs pass through unchanged.

    If more than *max_math_paragraphs* paragraphs contain math, processing
    is skipped entirely to avoid excessive LLM calls — the post-processing
    _latexify_result step will still normalize the output.
    """
    if not text or not has_math(text):
        return text

    paragraphs = _split_paragraphs(text)
    math_count = sum(1 for p in paragraphs if has_math(p) and len(p.strip()) > 20)
    if math_count > max_math_paragraphs:
        logger.info(
            "Skipping latexify: %d math paragraphs exceeds limit %d",
            math_count, max_math_paragraphs,
        )
        return text

    if llm_client is None:
        from reportagent.llm.client import LLMClient
        llm_client = LLMClient()

    result_parts: list[str] = []
    batch: list[int] = []  # indices of paragraphs to process
    batch_chars = 0

    for i, para in enumerate(paragraphs):
        if has_math(para) and len(para.strip()) > 20:
            batch.append(i)
            batch_chars += len(para)
            if batch_chars >= max_chars_per_chunk:
                processed = await _latexify_batch(
                    [paragraphs[j] for j in batch], batch, title, llm_client
                )
                for bi, pi in enumerate(batch):
                    paragraphs[pi] = processed[bi] if bi < len(processed) else paragraphs[pi]
                batch = []
                batch_chars = 0
        else:
            result_parts.append(para)

    # Process remaining batch
    if batch:
        processed = await _latexify_batch(
            [paragraphs[j] for j in batch], batch, title, llm_client
        )
        for bi, pi in enumerate(batch):
            paragraphs[pi] = processed[bi] if bi < len(processed) else paragraphs[pi]

    return "\n\n".join(paragraphs)


async def _latexify_batch(
    paragraphs: list[str],
    indices: list[int],
    title: str,
    llm_client,
) -> list[str]:
    """Send a batch of math-containing paragraphs to the LLM for LaTeX conversion.

    Uses a delimiter-based output format (instead of JSON) to avoid escaping
    issues with backslashes in LaTeX content.
    """
    numbered = "\n\n".join(
        f"<p idx={i}>{p}</p>" for i, p in zip(indices, paragraphs)
    )

    prompt = (
        "你是一位数学排版专家。以下是一篇量化金融论文中的段落，"
        "其中包含用普通 Unicode 字符表示的数学公式（如 α, β, r_{i,t} 等）。\n\n"
        "请将每个段落中的数学表达式转换为标准 LaTeX 行内公式格式："
        "用 $...$ 包裹每个独立的数学表达式。\n\n"
        "转换规则：\n"
        "1. 希腊字母: α→\\alpha, β→\\beta, γ→\\gamma, σ→\\sigma, "
        "ε→\\epsilon, μ→\\mu, Σ→\\Sigma, Π→\\Pi, ∏→\\prod, ∑→\\sum 等\n"
        "2. 下标: r_{i,t}, R_{m,t} 等，用 _ 表示下标，{} 包裹\n"
        "3. 上标: \\sigma^2, r_t^2 等，用 ^ 表示\n"
        "4. 分式: \\frac{分子}{分母}\n"
        "5. 每个完整的数学表达式单独用 $...$ 包裹\n"
        "6. 不要改动非数学的中文/英文内容\n"
        "7. 保持段落结构和标点符号不变\n\n"
        f"论文标题：{title}\n\n"
        f"待转换段落：\n\n{numbered}\n\n"
        "请严格按以下格式输出每个转换后的段落（格式中不可修改）：\n"
        "---IDX 0---\n"
        "转换后的段落文本...\n"
        "---IDX 1---\n"
        "转换后的段落文本...\n"
        "注意：---IDX N--- 是固定分隔符，必须原样出现在每段之前。"
        "不要输出任何其他内容。"
    )

    try:
        raw = await llm_client.chat(
            [
                {"role": "system", "content": "你是数学LaTeX排版专家。请严格按格式输出。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=3000,
        )
        return _parse_delimited_output(raw, indices, paragraphs)
    except Exception as e:
        logger.warning("LaTeX inline conversion failed for batch: %s", e)
        return paragraphs


def _parse_delimited_output(
    raw: str, indices: list[int], originals: list[str]
) -> list[str]:
    """Parse LLM output with ---IDX N--- delimiters into paragraph list."""
    import re as _re

    result_map: dict[int, str] = {}
    # Split on the delimiter pattern
    parts = _re.split(r"---IDX\s*(\d+)\s*---", raw)

    # parts will be: [prefix, idx1, content1, idx2, content2, ...]
    i = 1
    while i < len(parts):
        try:
            idx = int(parts[i].strip())
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if content:
                result_map[idx] = content
        except (ValueError, IndexError):
            pass
        i += 2

    return [result_map.get(idx, originals[j]) for j, idx in enumerate(indices)]


def _split_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs, keeping the structure."""
    # Split on double newlines (paragraph breaks)
    raw = re.split(r"\n\s*\n", text)
    return [p.strip() for p in raw if p.strip()]
