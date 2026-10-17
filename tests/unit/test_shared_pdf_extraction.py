"""Real PDF readers share bytes/path semantics and optional parser preferences."""

from types import SimpleNamespace

import fitz
import pytest

from reportagent.processors import mineru_parser
from reportagent.processors.mineru_parser import ParsedContent
from reportagent.processors.pdf_extractor import PDFExtractor
from reportagent.services.pdf_extraction import parse_pdf, parse_pdf_bytes


@pytest.fixture
def pdf(tmp_path, monkeypatch):
    path = tmp_path / "two-pages.pdf"
    with fitz.open() as document:
        document.set_metadata({"title": "Original title", "author": "Offline author"})
        document.new_page().insert_text((40, 40), "First research page")
        document.new_page().insert_text((40, 40), "Second research page")
        document.save(path)

    def unavailable(*args):
        raise ImportError("MinerU disabled for offline test")

    monkeypatch.setattr(mineru_parser, "_run_mineru_parse", unavailable)
    return path


def test_bytes_and_path_reader_keep_pages_metadata_and_text(pdf):
    extractor = PDFExtractor()
    path_result = extractor.extract(pdf)
    bytes_result = extractor.extract_bytes(pdf.read_bytes())
    assert (
        path_result.text
        == bytes_result.text
        == "First research page\n\nSecond research page\n"
    )
    assert (
        path_result.first_page_text
        == bytes_result.first_page_text
        == "First research page\n"
    )
    assert path_result.page_count == bytes_result.page_count == 2
    assert bytes_result.metadata["title"] == "Original title"
    assert bytes_result.metadata["author"] == "Offline author"
    assert (
        path_result.file_size_bytes
        == bytes_result.file_size_bytes
        == pdf.stat().st_size
    )


@pytest.mark.parametrize("use_mineru", [True, False])
def test_bytes_parser_returns_real_text_with_optional_mineru_fallback(pdf, use_mineru):
    result = parse_pdf_bytes(pdf.read_bytes(), use_mineru=use_mineru)
    assert result.text == "First research page\n\nSecond research page\n"
    assert result.page_count == 2
    assert result.tables == result.equations == []
    assert result.file_path == ""


def test_docling_policy_merges_tables_but_keeps_mineru_equations(pdf, monkeypatch):
    monkeypatch.setattr(
        mineru_parser,
        "_run_mineru_parse",
        lambda *args: [
            {"type": "text", "text": "MinerU text"},
            {"type": "table", "table_body": "MinerU table"},
            {"type": "equation", "latex": "x^2"},
        ],
    )
    secondary = SimpleNamespace(
        parse=lambda path: ParsedContent(
            text="Docling richer markdown " * 20,
            tables=[{"table_body": "Docling table"}],
            equations=[{"latex": "secondary"}],
        )
    )
    result = parse_pdf(pdf, docling_parser=secondary)
    assert result.text == "Docling richer markdown " * 20
    assert result.tables == [{"table_body": "Docling table"}]
    assert result.equations == [{"latex": "x^2", "text": "", "page_idx": 0}]


def test_failed_optional_docling_keeps_successful_primary_pdf(pdf):
    def unavailable(path):
        raise ImportError("Docling disabled for offline test")

    result = parse_pdf(pdf, docling_parser=SimpleNamespace(parse=unavailable))
    assert result.text == "First research page\n\nSecond research page\n"
    assert result.page_count == 2


def test_optional_parser_failure_keeps_docling_merge_after_plain_text_fallback(pdf):
    def failed(path):
        raise RuntimeError("optional parser failed")

    secondary = SimpleNamespace(
        parse=lambda path: ParsedContent(
            text="Docling evidence " * 20,
            tables=[{"table_body": "recovered table"}],
        )
    )
    result = parse_pdf(
        pdf, mineru_parser=SimpleNamespace(parse=failed), docling_parser=secondary
    )
    assert result.text == "Docling evidence " * 20
    assert result.tables == [{"table_body": "recovered table"}]
