"""Shared base extraction and optional structured PDF parsing policies."""

import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from reportagent.processors.mineru_parser import MinerUParser, ParsedContent
from reportagent.processors.pdf_extractor import PDFExtractor

logger = logging.getLogger(__name__)


def extract_text_pymupdf(pdf_path: str) -> str:
    """Extract verbatim text using the common PyMuPDF reader."""
    return PDFExtractor().extract(pdf_path).text


def parse_pdf(
    pdf_path: str | Path,
    *,
    use_mineru: bool = True,
    mineru_parser=None,
    docling_parser=None,
    base_content=None,
) -> ParsedContent:
    """Select structured parsing; Docling is an explicitly supplied merge policy.

    MinerUParser.parse owns its fallback, including empty MinerU output. Callers
    can reuse their optional parser instances without calling private builders.
    """
    parsed = None
    if use_mineru:
        try:
            parsed = (mineru_parser or MinerUParser()).parse(pdf_path)
        except Exception as error:
            logger.warning("Optional MinerU parser failed: %s", error)
    if parsed is None or not parsed.full_text_with_tables.strip():
        content = base_content or PDFExtractor().extract(pdf_path)
        parsed = ParsedContent(
            text=content.text, page_count=content.page_count, file_path=str(pdf_path)
        )
    if docling_parser is not None:
        try:
            secondary = docling_parser.parse(pdf_path)
            # Preserve the existing local import preference for richer markdown,
            # equally complete tables, and MinerU's equation transcription.
            if (
                secondary.text
                and len(secondary.text) > len(parsed.full_text_with_tables) * 0.5
            ):
                parsed.text = secondary.text
            if secondary.tables and len(secondary.tables) >= len(parsed.tables):
                parsed.tables = secondary.tables
            if secondary.equations and not parsed.equations:
                parsed.equations = secondary.equations
        except Exception as error:
            logger.warning("Docling secondary parse failed: %s", error)
    return parsed


def parse_pdf_bytes(data: bytes, *, use_mineru: bool = True) -> ParsedContent:
    if not use_mineru:
        content = PDFExtractor().extract_bytes(data)
        return ParsedContent(text=content.text, page_count=content.page_count)
    with TemporaryDirectory() as directory:
        path = Path(directory) / "report.pdf"
        path.write_bytes(data)
        parsed = parse_pdf(path)
        parsed.file_path = ""
        return parsed


def extract_formulas_mineru(pdf_path: str) -> tuple[list, list, str]:
    """Compatibility tuple adapter over the public structured parser."""
    parsed = parse_pdf(pdf_path)
    return parsed.tables, parsed.equations, parsed.full_text_with_tables
