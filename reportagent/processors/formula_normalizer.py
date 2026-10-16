"""Normalize LaTeX formulas produced by PDF parsers (MinerU, Docling).

PDF equation extraction often produces verbose LaTeX with artifacts from
PDF drawing primitives: \\mathopen {}, \\mathclose \\bgroup...\\aftergroup \\egroup,
\\operatorname*{min} instead of \\min, and excessive whitespace in braces.
"""

from __future__ import annotations

import re


# Patterns that are pure PDF artifacts — safe to remove entirely
_ARTIFACT_PATTERNS: list[tuple[str, str]] = [
    # \mathopen {} — empty mathopen (PDF left-delimiter artifact)
    (r"\\mathopen\s*\{\s*\}", ""),
    # \mathclose \bgroup — PDF right-delimiter opening
    (r"\\mathclose\s*\\bgroup\s*", ""),
    # \aftergroup \egroup — PDF right-delimiter closing
    (r"\\aftergroup\s*\\egroup\s*", ""),
    # \big. — empty big operator (size-only, no actual delimiter)
    (r"\\big\s*\.", ""),
]

# Verbose operator names → standard form
_OPERATOR_MAP: list[tuple[str, str]] = [
    (r"\\operatorname\*\s*\{min\}", r"\\min"),
    (r"\\operatorname\*\s*\{max\}", r"\\max"),
    (r"\\operatorname\s*\{min\}", r"\\min"),
    (r"\\operatorname\s*\{max\}", r"\\max"),
    (r"\\operatorname\*\s*\{argmax\}", r"\\argmax"),
    (r"\\operatorname\*\s*\{argmin\}", r"\\argmin"),
    (r"\\operatorname\s*\{argmax\}", r"\\argmax"),
    (r"\\operatorname\s*\{argmin\}", r"\\argmin"),
    (r"\\operatorname\*\s*\{s\.t\.\}", r"\\text{s.t.}"),
    (r"\\operatorname\s*\{s\.t\.\}", r"\\text{s.t.}"),
    (r"\\operatorname\*\s*\{rank\}", r"\\rank"),
    (r"\\operatorname\s*\{rank\}", r"\\rank"),
    (r"\\operatorname\*\s*\{tr\}", r"\\tr"),
    (r"\\operatorname\s*\{tr\}", r"\\tr"),
    (r"\\operatorname\*\s*\{diag\}", r"\\diag"),
    (r"\\operatorname\s*\{diag\}", r"\\diag"),
    (r"\\operatorname\*\s*\{sign\}", r"\\sign"),
    (r"\\operatorname\s*\{sign\}", r"\\sign"),
    (r"\\operatorname\*\s*\{cov\}", r"\\cov"),
    (r"\\operatorname\s*\{cov\}", r"\\cov"),
    (r"\\operatorname\*\s*\{var\}", r"\\var"),
    (r"\\operatorname\s*\{var\}", r"\\var"),
    (r"\\operatorname\*\s*\{corr\}", r"\\corr"),
    (r"\\operatorname\s*\{corr\}", r"\\corr"),
    (r"\\operatorname\*\s*\{supp\}", r"\\supp"),
    (r"\\operatorname\s*\{supp\}", r"\\supp"),
    (r"\\operatorname\*\s*\{span\}", r"\\span"),
    (r"\\operatorname\s*\{span\}", r"\\span"),
]


def normalize_formula(latex: str) -> str:
    """Clean up PDF-extraction artifacts in a LaTeX formula string.

    Handles:
    - \\mathopen {} / \\mathclose \\bgroup...\\aftergroup \\egroup (delimiter artifacts)
    - \\operatorname*{min} → \\min (and similar operator normalizations)
    - Whitespace normalization inside braces
    """
    if not latex or not latex.strip():
        return latex

    # 1. Strip pure artifacts
    for pattern, replacement in _ARTIFACT_PATTERNS:
        latex = re.sub(pattern, replacement, latex)

    # 2. Normalize operator names
    for pattern, replacement in _OPERATOR_MAP:
        latex = re.sub(pattern, replacement, latex)

    # 3. Normalize whitespace inside braces: { \mathbf { w } } → {\mathbf{w}}
    latex = _normalize_brace_spaces(latex)

    # 4. Remove spaces between command and its brace argument: \hat {x} → \hat{x}
    latex = re.sub(r"(\\[a-zA-Z]+)\s+(\{)", r"\1\2", latex)

    # 5. Remove spaces before subscripts/superscripts: x _ {i} → x_{i}
    latex = re.sub(r"\s+([_^])\s*(\{)", r"\1\2", latex)
    latex = re.sub(r"\s+([_^])\s*([a-zA-Z0-9])", r"\1\2", latex)

    # 6. Collapse runs of spaces (LaTeX math mode ignores them anyway)
    latex = re.sub(r"[ \t]+", " ", latex)

    # 7. Remove leading/trailing whitespace
    latex = latex.strip()

    return latex


def _normalize_brace_spaces(latex: str) -> str:
    """Normalize whitespace inside LaTeX brace groups.

    { w } -> {w}
    { \\mathbf { w } } -> {\\mathbf{w}}
    But preserves intentional spaces in multi-word text: {s.t.} stays.
    """
    # Repeatedly normalize innermost braces until stable
    prev = None
    while prev != latex:
        prev = latex
        latex = _pass_normalize_braces(latex)
    return latex


def _pass_normalize_braces(latex: str) -> str:
    """Single pass: normalize the content of each brace group."""
    result = []
    i = 0
    while i < len(latex):
        if latex[i] == "{":
            depth = 1
            j = i + 1
            while j < len(latex) and depth > 0:
                if latex[j] == "{":
                    depth += 1
                elif latex[j] == "}":
                    depth -= 1
                j += 1
            inner = latex[i + 1 : j - 1]
            # If inner is a single LaTeX command or single token, strip spaces
            stripped = inner.strip()
            if stripped and not " " in stripped:
                result.append("{" + stripped + "}")
            elif stripped and len(stripped) < 3:
                result.append("{" + stripped + "}")
            else:
                result.append("{" + stripped + "}")
            i = j
        else:
            result.append(latex[i])
            i += 1
    return "".join(result)


def normalize_formulas(formulas: list[dict]) -> list[dict]:
    """Normalize LaTeX in a list of formula dicts (with 'latex' key)."""
    for f in formulas:
        if isinstance(f, dict) and f.get("latex"):
            f["latex"] = normalize_formula(f["latex"])
    return formulas
