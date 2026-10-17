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
    assert result.first_page_text == "First research page\n"


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


@pytest.mark.parametrize("first_page", ["OCR first-page title\nAbstract", ""])
def test_selected_structured_text_keeps_its_matching_first_page(pdf, first_page):
    primary = ParsedContent(
        text="Primary text", first_page_text="Primary first page", page_count=1
    )
    secondary = ParsedContent(
        text="Docling full text " * 20, first_page_text=first_page, page_count=2
    )

    result = parse_pdf(
        pdf,
        mineru_parser=SimpleNamespace(parse=lambda _: primary),
        docling_parser=SimpleNamespace(parse=lambda _: secondary),
    )

    assert result.text == secondary.text
    assert result.first_page_text == first_page


def test_docling_first_page_uses_one_based_provenance_and_includes_headings():
    from reportagent.processors.docling_parser import DoclingParser

    items = [
        SimpleNamespace(
            label=label,
            text=text,
            prov=[SimpleNamespace(page_no=page)],
            self_ref=str(i),
        )
        for i, (label, text, page) in enumerate(
            [
                ("title", "OCR paper title", 1),
                ("text", "arXiv: 2501.12345", 1),
                ("section_header", "Abstract", 1),
                ("text", "First-page abstract", 1),
                ("text", "doi: 10.4321/cited", 2),
            ]
        )
    ]
    doc = SimpleNamespace(
        texts=items,
        iterate_items=lambda: ((item, 0) for item in items),
        export_to_markdown=lambda: "\n".join(item.text for item in items),
    )

    result = DoclingParser()._build_result(doc, "paper.pdf")

    assert result.first_page_text == (
        "OCR paper title\narXiv: 2501.12345\nAbstract\nFirst-page abstract"
    )
    assert "10.4321/cited" in result.text


def test_mineru_known_empty_first_page_is_not_replaced_by_second_page():
    result = mineru_parser.MinerUParser()._build_result(
        [{"type": "text", "text": "Second-page text", "page_idx": 1}], "paper.pdf"
    )

    assert result.text == "Second-page text"
    assert result.first_page_text == ""
