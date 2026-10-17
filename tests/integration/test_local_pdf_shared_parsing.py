"""Local structured parsing keeps the actual PDF's metadata and section evidence."""

import json
from datetime import datetime
from types import SimpleNamespace

import fitz

from reportagent.models.schemas import UserCriteria
from reportagent.processors.mineru_parser import ParsedContent
from reportagent.sources import local_pdf


def test_optional_structured_parsing_preserves_pdf_metadata_date_and_abstract(
    tmp_path, monkeypatch
):
    path = tmp_path / "paper.pdf"
    with fitz.open() as document:
        document.set_metadata(
            {
                "title": "Verified PDF title",
                "author": "Original Author",
                "creationDate": "D:20261017120000",
            }
        )
        document.new_page().insert_text(
            (40, 40),
            "Native title\nPublished: 2026-01-15\nAbstract\nNative abstract material.\n"
            + "Research methods and results.\n" * 8,
        )
        document.save(path)
    primary = ParsedContent(
        text="Structured title\nAbstract\nMinerU abstract.\n1 Introduction\n"
        + "Primary research material. " * 8,
        tables=[{"table_body": "primary table"}],
        equations=[{"latex": "x^2"}],
    )
    secondary = ParsedContent(
        text="Docling title\nAbstract\nVerified structured abstract.\n1 Introduction\n"
        + "Structured research evidence. " * 12,
        tables=[{"table_body": "structured table"}],
        equations=[{"latex": "secondary"}],
    )
    monkeypatch.setattr(
        local_pdf,
        "_get_mineru_parser",
        lambda: SimpleNamespace(parse=lambda path: primary),
    )
    monkeypatch.setattr(
        local_pdf,
        "_get_docling_parser",
        lambda: SimpleNamespace(parse=lambda path: secondary),
    )
    source = local_pdf.LocalPDFSource(str(tmp_path))
    row = source._process_pdf(
        path,
        UserCriteria(
            topics=["factor"],
            date_from=datetime(2026, 1, 1),
            date_to=datetime(2026, 1, 31),
        ),
    )
    assert row is not None
    assert row.title == "Verified PDF title"
    assert row.authors == ["Original Author"]
    assert row.published_date == datetime(2026, 1, 15)
    assert row.abstract == "Verified structured abstract."
    assert json.loads(row.tables_json) == [{"table_body": "structured table"}]
    assert json.loads(row.equations_json)[0]["latex"] == "x^2"
    assert row.raw_metadata["publication_date_source"] == "labelled_publication_date"
    assert row.raw_metadata["pdf_creation_date"] == "D:20261017120000"
