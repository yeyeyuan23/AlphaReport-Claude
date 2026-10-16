from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ParsedContent:
    text: str
    tables: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    equations: list[dict] = field(default_factory=list)
    page_count: int = 0
    content_list: list[dict] = field(default_factory=list)
    file_path: str = ""

    @property
    def full_text_with_tables(self) -> str:
        parts = [self.text]
        for t in self.tables:
            caption = ", ".join(t.get("table_caption", []))
            body = t.get("table_body", "")
            if caption:
                parts.append(f"\n[Table: {caption}]\n{body}")
            elif body:
                parts.append(f"\n[Table]\n{body}")
        for eq in self.equations:
            latex = eq.get("latex", "")
            if latex:
                parts.append(f"\n[Equation] {latex}")
        return "\n".join(parts)


def _run_mineru_parse(pdf_path: str, output_dir: str) -> list[dict]:
    import json
    from mineru.cli.common import do_parse, prepare_env

    pdf_name = Path(pdf_path).stem
    prepare_env(output_dir, pdf_name, parse_method="auto")

    pdf_bytes = Path(pdf_path).read_bytes()

    do_parse(
        output_dir=output_dir,
        pdf_file_names=[pdf_name],
        pdf_bytes_list=[pdf_bytes],
        p_lang_list=[""],
        parse_method="auto",
        backend="pipeline",
        formula_enable=True,
        table_enable=True,
        f_draw_layout_bbox=False,
        f_draw_span_bbox=False,
        f_dump_md=False,
        f_dump_model_output=False,
        f_dump_orig_pdf=False,
        f_dump_content_list=True,
        f_dump_middle_json=False,
    )

    content_list_dir = Path(output_dir) / pdf_name / "auto"
    content_list_file = content_list_dir / f"{pdf_name}_content_list.json"
    if not content_list_file.exists():
        for f in Path(output_dir).rglob("*_content_list.json"):
            content_list_file = f
            break
    if not content_list_file.exists():
        for f in Path(output_dir).rglob("content_list.json"):
            content_list_file = f
            break

    if content_list_file.exists():
        return json.loads(content_list_file.read_text(encoding="utf-8"))
    return []


def _extract_latex_from_text(text: str) -> str:
    """Extract clean LaTeX from MinerU's text field which wraps equations in $$...$$.

    MinerU inserts spaces between every character (e.g. 'r e t 2 0' instead of 'ret20')
    and uses \\_ for underscores. This cleans those artifacts via a token-based
    approach that preserves LaTeX command boundaries.
    """
    import re

    t = text.strip()
    # Remove outer $$ delimiters
    if t.startswith("$$") and t.endswith("$$"):
        t = t[2:-2]
    elif t.startswith("$") and t.endswith("$"):
        t = t[1:-1]
    # Remove \\tag{...} markers
    t = re.sub(r'\\tag\{[^}]*\}', '', t)
    # Collapse excessive newlines
    t = re.sub(r'\n{3,}', '\n\n', t)

    # Token-based cleaning: split on spaces, merge runs of single-char tokens
    # (like "r e t 2 0") but preserve LaTeX commands (tokens starting with \)
    tokens = t.split(' ')
    result = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith('\\') or len(tok) != 1:
            result.append(tok)
            i += 1
        else:
            run = [tok]
            j = i + 1
            while j < len(tokens):
                nxt = tokens[j]
                if nxt.startswith('\\') or len(nxt) != 1:
                    break
                run.append(nxt)
                j += 1
            result.append(''.join(run))
            i = j
    t = ' '.join(result)

    # Fix \\_ to _ (MinerU uses escaped underscore; in math mode _ is subscript)
    t = t.replace('\\_', '_')

    # Remove spaces inside braces
    t = re.sub(r'\{\s+', '{', t)
    t = re.sub(r'\s+\}', '}', t)

    # Fix ". " / "." artifacts that should be underscores in variable names
    t = re.sub(r'([a-zA-Z0-9]) \. ([a-zA-Z0-9])', r'\1_\2', t)
    t = re.sub(r'([a-zA-Z])\.([a-zA-Z])', r'\1_\2', t)

    # Remove spaces before ^ and _
    t = re.sub(r'\s*\^\s*', '^', t)
    t = re.sub(r'\s*_\s*', '_', t)

    return t.strip()


class MinerUParser:
    def parse(self, pdf_path: str | Path) -> ParsedContent:
        pdf_path = str(pdf_path)

        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                content_list = _run_mineru_parse(pdf_path, tmp_dir)
            except Exception as e:
                logger.warning("MinerU parse failed for %s: %s, falling back to PyMuPDF", pdf_path, e)
                return self._fallback_parse(pdf_path)

        return self._build_result(content_list, pdf_path)

    async def aparse(self, pdf_path: str | Path) -> ParsedContent:
        return await asyncio.to_thread(self.parse, pdf_path)

    def _build_result(self, content_list: list[dict], pdf_path: str) -> ParsedContent:
        text_parts: list[str] = []
        tables: list[dict] = []
        images: list[dict] = []
        equations: list[dict] = []
        max_page = 0

        for item in content_list:
            item_type = item.get("type", "")
            page_idx = item.get("page_idx", 0)
            max_page = max(max_page, page_idx)

            if item_type == "text":
                text_parts.append(item.get("text", ""))
            elif item_type == "table":
                tables.append({
                    "table_body": item.get("table_body", ""),
                    "table_caption": item.get("table_caption", []),
                    "page_idx": page_idx,
                })
            elif item_type == "image":
                images.append({
                    "img_path": item.get("img_path", ""),
                    "image_caption": item.get("image_caption", []),
                    "page_idx": page_idx,
                })
            elif item_type == "equation":
                latex = item.get("latex", "")
                text = item.get("text", "")
                # MinerU often puts LaTeX in the `text` field wrapped in $$...$$
                if not latex and text:
                    latex = _extract_latex_from_text(text)
                equations.append({
                    "latex": latex,
                    "text": text,
                    "page_idx": page_idx,
                })

        return ParsedContent(
            text="\n".join(text_parts),
            tables=tables,
            images=images,
            equations=equations,
            page_count=max_page + 1,
            content_list=content_list,
            file_path=pdf_path,
        )

    def _fallback_parse(self, pdf_path: str) -> ParsedContent:
        import fitz
        doc = fitz.open(pdf_path)
        try:
            text_parts = [page.get_text() for page in doc]
            return ParsedContent(
                text="\n".join(text_parts),
                page_count=doc.page_count,
                file_path=pdf_path,
            )
        finally:
            doc.close()
