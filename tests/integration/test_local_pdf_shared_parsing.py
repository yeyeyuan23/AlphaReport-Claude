"""Local structured parsing keeps the actual PDF's metadata and section evidence."""

from reportagent.sources.date_window import source_date


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
    assert row.published_date == source_date(datetime(2026, 1, 15))
    assert row.abstract == "Verified structured abstract."
    assert json.loads(row.tables_json) == [{"table_body": "structured table"}]
    assert json.loads(row.equations_json)[0]["latex"] == "x^2"
    assert row.raw_metadata["publication_date_source"] == "labelled_publication_date"
    assert row.raw_metadata["pdf_creation_date"] == "D:20261017120000"


def test_docling_table_is_not_duplicated_in_full_review_material(tmp_path, monkeypatch):
    import asyncio

    import pytest

    from reportagent.services.report_quality import evaluate_paper

    path = tmp_path / "large-paper.pdf"
    with fitz.open() as document:
        document.new_page().insert_text(
            (40, 40), "Factor research title\n" + "Research evidence. " * 15
        )
        document.save(path)
    table = "| factor_id | training_return | test_return |\n" + (
        "| factor_00001 | 0.012345 | 0.006789 |\n" * 2500
    )
    markdown = "Research evidence and methodology. " * 3000 + "\n" + table
    monkeypatch.setattr(
        local_pdf,
        "_get_mineru_parser",
        lambda: SimpleNamespace(
            parse=lambda _: ParsedContent(text="Primary evidence. " * 10)
        ),
    )
    monkeypatch.setattr(
        local_pdf,
        "_get_docling_parser",
        lambda: SimpleNamespace(
            parse=lambda _: ParsedContent(text=markdown, tables=[{"table_body": table}])
        ),
    )

    row = local_pdf.LocalPDFSource(str(tmp_path))._process_pdf(
        path, UserCriteria(topics=["factor"])
    )

    assert len(markdown) == 202547
    assert row.full_text == markdown
    assert row.full_text.count(table.strip()) == 1
    assert json.loads(row.tables_json) == [{"table_body": table}]

    class ModelReached(RuntimeError):
        pass

    class FakeModel:
        async def chat_json(self, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            assert payload["text_complete"] is True
            raise ModelReached("Offline full-text evaluation reached")

    with pytest.raises(ModelReached):
        asyncio.run(
            evaluate_paper(
                {
                    "title": row.title,
                    "full_text": row.full_text,
                    "has_full_text": True,
                    "source": "local_pdf",
                },
                FakeModel(),
            )
        )
