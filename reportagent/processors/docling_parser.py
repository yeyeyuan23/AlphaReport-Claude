from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from reportagent.processors.mineru_parser import ParsedContent

logger = logging.getLogger(__name__)


class DoclingParser:
    """Parse PDFs via IBM Docling — better table structure, reading order, and markdown output.

    Complements MinerU: MinerU gives better LaTeX for formulas; Docling gives better
    tables, section structure, and clean markdown text. Use both and merge results.
    """

    def __init__(self, enable_ocr: bool = True):
        self._enable_ocr = enable_ocr

    def parse(self, pdf_path: str | Path) -> ParsedContent:
        from docling.document_converter import DocumentConverter

        pdf_path = str(pdf_path)

        try:
            converter = DocumentConverter()
            result = converter.convert(pdf_path)
            doc = result.document
        except Exception as e:
            logger.warning("Docling parse failed for %s: %s", pdf_path, e)
            return self._empty_result(pdf_path)

        return self._build_result(doc, pdf_path)

    async def aparse(self, pdf_path: str | Path) -> ParsedContent:
        return await asyncio.to_thread(self.parse, pdf_path)

    def _build_result(self, doc, pdf_path: str) -> ParsedContent:
        tables: list[dict] = []
        equations: list[dict] = []
        images: list[dict] = []
        max_page = 0

        # Build text lookup for formula self_refs
        text_lookup = {}
        for t in doc.texts:
            text_lookup[t.self_ref] = t
            if t.prov:
                for p in t.prov:
                    max_page = max(max_page, p.page_no)

        for item, _level in doc.iterate_items():
            page_no = item.prov[0].page_no if item.prov else 0
            max_page = max(max_page, page_no)

            if item.label == "table":
                body = item.export_to_markdown(doc=doc) if hasattr(item, 'export_to_markdown') else ""
                caption = ""
                if hasattr(item, 'caption_text') and callable(item.caption_text):
                    caption = item.caption_text(doc) or ""
                tables.append({
                    "table_body": body,
                    "table_caption": [caption] if caption else [],
                    "page_idx": page_no,
                })

            elif item.label == "formula":
                # Docling stores formula text in `orig`, not `text`
                orig = getattr(item, 'orig', '') or ''
                text_val = item.text if hasattr(item, 'text') else ''
                latex = orig or text_val
                # Try to get LaTeX from the referenced text item
                if hasattr(item, 'self_ref') and item.self_ref:
                    txt = text_lookup.get(item.self_ref)
                    if txt and getattr(txt, 'orig', ''):
                        orig_text = txt.orig or ''
                        if orig_text.strip():
                            latex = orig_text
                equations.append({
                    "latex": latex,
                    "text": text_val or orig,
                    "page_idx": page_no,
                })

            elif item.label == "picture":
                caption = ""
                if hasattr(item, 'caption_text') and callable(item.caption_text):
                    caption = item.caption_text(doc) or ""
                images.append({
                    "img_path": "",
                    "image_caption": [caption] if caption else [],
                    "page_idx": page_no,
                })

        # Full text as clean markdown
        try:
            full_md = doc.export_to_markdown()
        except Exception:
            full_md = "\n".join(t.text for t in doc.texts if t.text)

        return ParsedContent(
            text=full_md,
            tables=tables,
            images=images,
            equations=equations,
            page_count=max_page,
            content_list=self._build_content_list(doc),
            file_path=pdf_path,
        )

    def _build_content_list(self, doc) -> list[dict]:
        """Build a MinerU-compatible content_list from Docling items."""
        items: list[dict] = []
        for item, _level in doc.iterate_items():
            page_no = item.prov[0].page_no if item.prov else 0
            label = str(item.label)

            entry: dict = {"type": label, "page_idx": page_no}

            if label == "text":
                entry["text"] = item.text if hasattr(item, 'text') else ''
            elif label == "table":
                try:
                    entry["table_body"] = item.export_to_markdown(doc=doc)
                except Exception:
                    entry["table_body"] = ""
                entry["table_caption"] = []
            elif label == "formula":
                entry["latex"] = getattr(item, 'orig', '') or item.text if hasattr(item, 'text') else ''
                entry["text"] = item.text if hasattr(item, 'text') else ''
            elif label == "picture":
                entry["img_path"] = ""
                entry["image_caption"] = []

            items.append(entry)

        return items

    @staticmethod
    def _empty_result(pdf_path: str) -> ParsedContent:
        return ParsedContent(text="", file_path=pdf_path, page_count=0)
