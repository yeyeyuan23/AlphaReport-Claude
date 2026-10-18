"""Native and OCR front matter must describe one paper, not a cited paper."""

from reportagent.sources.date_window import source_date


from datetime import datetime
from types import SimpleNamespace

import fitz
import pytest

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Report
from reportagent.models.schemas import (
    ClassificationResult,
    ClassifiedReport,
    UserCriteria,
)
from reportagent.processors.mineru_parser import MinerUParser, ParsedContent
from reportagent.sources import local_pdf


BODY = "\nAbstract\n" + "Research evidence and limitations.\n" * 12
OCR_FRONT = (
    "Factor portfolio research\nPublished: 2026-01-15\n"
    "arXiv: 2501.12345v1\ndoi: 10.1234/portfolio\n"
)


@pytest.fixture
def db(memory_session_factory):
    with memory_session_factory() as session:
        yield session


def write_pdf(path, native, *, metadata=None, scanned=None):
    with fitz.open() as document:
        page = document.new_page()
        if scanned:
            with fitz.open() as image_document:
                image_document.new_page().insert_text((40, 60), scanned)
                image = image_document[0].get_pixmap().tobytes("png")
            page.insert_image(page.rect, stream=image)
        page.insert_text((40, 25), native)
        if metadata:
            document.set_metadata(metadata)
        document.save(path)


def source_with_ocr(tmp_path, monkeypatch, ocr):
    monkeypatch.setattr(
        local_pdf,
        "_get_mineru_parser",
        lambda: SimpleNamespace(
            parse=lambda path: ocr
            if isinstance(ocr, ParsedContent)
            else ParsedContent(text=ocr(path) if callable(ocr) else ocr)
        ),
    )
    monkeypatch.setattr(local_pdf, "_get_docling_parser", lambda: None)
    return local_pdf.LocalPDFSource(str(tmp_path))


@pytest.mark.parametrize(
    "native",
    ["Digitized by Research Archive", "", "arXiv: 2501.12345v1"],
)
def test_scanned_front_matter_fills_each_missing_identity_field(
    tmp_path, monkeypatch, native
):
    path = tmp_path / "scan.pdf"
    write_pdf(path, native, scanned=OCR_FRONT + BODY)
    source = source_with_ocr(tmp_path, monkeypatch, OCR_FRONT + BODY)

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.title == "Factor portfolio research"
    assert row.arxiv_id == "2501.12345"
    assert row.doi == "10.1234/portfolio"
    assert row.published_date == source_date(datetime(2026, 1, 15))
    assert row.raw_metadata["publication_date_source"] == "labelled_publication_date"
    assert (
        row.raw_metadata["publication_date_info"]["evidence"] == "Published: 2026-01-15"
    )
    assert (
        source._process_pdf(
            path,
            UserCriteria(
                topics=["factor"],
                date_from=datetime(2026, 1, 1),
                date_to=datetime(2026, 1, 31),
            ),
        )
        is not None
    )


@pytest.mark.parametrize("embedded_title", [False, True])
def test_valid_native_title_is_preserved_while_ocr_fills_other_fields(
    tmp_path, monkeypatch, embedded_title
):
    path = tmp_path / "paper.pdf"
    write_pdf(
        path,
        "Digitized by Research Archive" if embedded_title else "Verified native title",
        metadata={"title": "Verified native title", "author": "Original Author"}
        if embedded_title
        else None,
    )
    source = source_with_ocr(tmp_path, monkeypatch, OCR_FRONT + BODY)

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.title == "Verified native title"
    assert row.arxiv_id == "2501.12345"
    assert row.doi == "10.1234/portfolio"
    assert row.published_date == source_date(datetime(2026, 1, 15))
    if embedded_title:
        assert row.authors == ["Original Author"]


@pytest.mark.parametrize(
    "native,arxiv_id,doi",
    [
        ("arXiv: 2401.54321v2", "2401.54321", None),
        ("doi: 10.4321/native", None, "10.4321/native"),
    ],
)
def test_identifier_conflict_does_not_create_a_mixed_paper_identity(
    tmp_path, monkeypatch, native, arxiv_id, doi
):
    path = tmp_path / "paper.pdf"
    write_pdf(path, "Verified native title\n" + native + BODY)
    source = source_with_ocr(tmp_path, monkeypatch, OCR_FRONT + BODY)

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.title == "Verified native title"
    assert row.arxiv_id == arxiv_id
    assert row.doi == doi
    assert row.published_date is None


@pytest.mark.parametrize(
    "native_date,expected_date,precision",
    [
        ("2025-02-03", datetime(2025, 2, 3), "day"),
        ("January 2026", datetime(2026, 1, 15), "day"),
        ("August 2025", None, "month"),
    ],
)
def test_publication_date_keeps_native_evidence_and_only_refines_compatible_dates(
    tmp_path, monkeypatch, native_date, expected_date, precision
):
    path = tmp_path / "paper.pdf"
    write_pdf(path, f"Verified native title\nPublished: {native_date}\n" + BODY)
    source = source_with_ocr(tmp_path, monkeypatch, OCR_FRONT + BODY)

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.published_date == source_date(expected_date)
    assert row.raw_metadata["publication_date_info"]["precision"] == precision
    if native_date == "August 2025":
        assert row.raw_metadata["publication_date_info"]["year"] == 2025
        assert row.raw_metadata["publication_date_info"]["month"] == 8


@pytest.mark.parametrize("native_layer", [False, True])
@pytest.mark.parametrize(
    "heading",
    ["Abstract", "## Abstract", "1 Introduction", "References", "## 5 References"],
)
def test_body_and_reference_identifiers_never_supply_paper_identity(
    tmp_path, monkeypatch, native_layer, heading
):
    text = (
        "Verified research title\n"
        + heading
        + "\nPrior work: arXiv: 2401.54321 and doi: 10.4321/cited\n"
        + "Published: 2020-01-01\n"
        + "Research evidence and limitations.\n" * 12
    )
    path = tmp_path / "paper.pdf"
    write_pdf(path, text if native_layer else "Digitized by Research Archive")
    source = source_with_ocr(tmp_path, monkeypatch, text)

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.title == "Verified research title"
    assert row.arxiv_id is None
    assert row.doi is None
    assert row.published_date is None


def test_different_watermarked_scans_remain_separate_database_records(
    tmp_path, monkeypatch, db
):
    texts = {}
    for number in (1, 2):
        path = tmp_path / f"paper{number}.pdf"
        texts[path] = (
            f"Factor portfolio research {number}\nPublished: 2026-01-15\n"
            f"arXiv: 2501.1234{number}v1\n" + BODY
        )
        write_pdf(path, "Digitized by Research Archive", scanned=texts[path])
    source = source_with_ocr(tmp_path, monkeypatch, texts.__getitem__)
    repository = ReportRepository(db)

    actions = []
    for path in texts:
        row = source._process_pdf(path, UserCriteria(topics=["factor"]))
        actions.append(
            repository.upsert_report(
                ClassifiedReport(
                    search_result=row, classification=ClassificationResult()
                )
            )[0]
        )

    assert actions == ["added", "added"]
    assert db.query(Report).count() == 2
    assert {row.arxiv_id for row in db.query(Report)} == {"2501.12341", "2501.12342"}


@pytest.mark.parametrize("with_page_provenance", [False, True])
def test_second_page_metadata_cannot_become_scanned_paper_identity(
    tmp_path, monkeypatch, with_page_provenance
):
    path = tmp_path / "two-pages.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((40, 25), "Digitized by Research Archive")
        document.new_page()
        document.save(path)
    front = "Verified research title\n" + "Unheaded research evidence.\n" * 12
    later = (
        "Prior work: arXiv: 2401.54321 and doi: 10.4321/cited\n"
        "Published: 2020-01-01\nIntroduction\nLater material."
    )
    parsed = (
        MinerUParser()._build_result(
            [
                {"type": "text", "text": front, "page_idx": 0},
                {"type": "text", "text": later, "page_idx": 1},
            ],
            str(path),
        )
        if with_page_provenance
        else ParsedContent(text=front + later)
    )
    source = source_with_ocr(tmp_path, monkeypatch, parsed)

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.arxiv_id is None
    assert row.doi is None
    assert row.published_date is None
    if with_page_provenance:
        assert row.title == "Verified research title"


def test_ambiguous_native_identifiers_are_not_treated_as_missing(tmp_path, monkeypatch):
    path = tmp_path / "paper.pdf"
    write_pdf(
        path,
        "Verified native title\narXiv: 2501.12345\narXiv: 2401.54321\n" + BODY,
    )
    source = source_with_ocr(tmp_path, monkeypatch, OCR_FRONT + BODY)

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.arxiv_id is None
    assert row.doi is None
    assert row.published_date is None


def test_native_arxiv_revision_footer_is_preserved_without_using_body_citations(
    tmp_path, monkeypatch
):
    path = tmp_path / "paper.pdf"
    write_pdf(
        path,
        "Verified native title"
        + BODY
        + "We compare arXiv: 2501.12345.\n"
        + "arXiv:2106.12950v2 [cs.LG] 25 Jun 2021\n",
    )
    source = source_with_ocr(tmp_path, monkeypatch, OCR_FRONT + BODY)

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.arxiv_id == "2106.12950"
    assert row.doi is None
    assert row.raw_metadata["publication_date_info"]["arxiv_version"] == "v2"


def test_case_variants_of_native_doi_are_not_ambiguous(tmp_path, monkeypatch):
    path = tmp_path / "paper.pdf"
    write_pdf(
        path,
        "Verified native title\ndoi: 10.1234/NATIVE\ndoi: 10.1234/native\n" + BODY,
    )
    source = source_with_ocr(tmp_path, monkeypatch, OCR_FRONT + BODY)

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.doi.lower() == "10.1234/native"
    assert row.arxiv_id is None
    assert row.published_date is None


def test_ambiguous_ocr_does_not_add_fields_to_a_valid_native_identity(
    tmp_path, monkeypatch
):
    path = tmp_path / "paper.pdf"
    write_pdf(path, "Verified native title\narXiv: 2501.12345v1\n" + BODY)
    source = source_with_ocr(
        tmp_path, monkeypatch, OCR_FRONT + "arXiv: 2401.54321v2\n" + BODY
    )

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.arxiv_id == "2501.12345"
    assert row.doi is None
    assert row.published_date is None


def test_same_arxiv_id_with_different_versions_still_allows_missing_fields(
    tmp_path, monkeypatch
):
    path = tmp_path / "paper.pdf"
    write_pdf(path, "Verified native title\narXiv: 2501.12345v1\narXiv: 2501.12345v2\n")
    source = source_with_ocr(tmp_path, monkeypatch, OCR_FRONT + BODY)

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.arxiv_id == "2501.12345"
    assert row.doi == "10.1234/portfolio"
    assert row.published_date == source_date(datetime(2026, 1, 15))


def test_known_blank_structured_first_page_never_uses_later_page_metadata(
    tmp_path, monkeypatch
):
    path = tmp_path / "blank-cover.pdf"
    with fitz.open() as document:
        document.new_page()
        document.new_page().insert_text((40, 25), OCR_FRONT + BODY)
        document.save(path)
    parsed = MinerUParser()._build_result(
        [{"type": "text", "page_idx": 1, "text": OCR_FRONT + BODY}], str(path)
    )
    source = source_with_ocr(tmp_path, monkeypatch, parsed)

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.title == "blank-cover"
    assert row.arxiv_id is None
    assert row.doi is None
    assert row.published_date is None


def test_ocr_revision_evidence_is_kept_without_inventing_publication_date(
    tmp_path, monkeypatch
):
    path = tmp_path / "paper.pdf"
    write_pdf(path, "Digitized by Research Archive")
    source = source_with_ocr(
        tmp_path,
        monkeypatch,
        "Verified research title" + BODY + "arXiv:2106.12950v2 [cs.LG] 25 Jun 2021\n",
    )

    row = source._process_pdf(path, UserCriteria(topics=["factor"]))

    assert row.arxiv_id == "2106.12950"
    assert row.published_date is None
    assert (
        row.raw_metadata["publication_date_info"]["arxiv_version_date"] == "2021-06-25"
    )
