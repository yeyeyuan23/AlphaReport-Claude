"""Local collection applies the same inclusive UTC date window as web sources."""

import asyncio
from datetime import date, datetime, timedelta, timezone

import fitz
import pytest

from reportagent.models.schemas import UserCriteria
from reportagent.sources import local_pdf


@pytest.fixture(autouse=True)
def offline_pdf_parsers(monkeypatch):
    monkeypatch.setattr(local_pdf, "_get_mineru_parser", lambda: None)
    monkeypatch.setattr(local_pdf, "_get_docling_parser", lambda: None)


def write_pdf(path, date_label):
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text(
            (72, 72),
            "Factor model research\n"
            + date_label
            + "\nAbstract\n"
            + "We study factor models and portfolio optimization.\n" * 8,
        )
        document.set_metadata(
            {"title": "Factor model research", "creationDate": "D:20261017120000"}
        )
        document.save(path)


@pytest.mark.parametrize(
    "label,start,end,included",
    [
        (
            "Published: 2020-01-01",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            None,
            False,
        ),
        ("Published: 2026-02-01", None, datetime(2026, 1, 31), False),
        ("Published: 2026-01-15", datetime(2026, 1, 1), datetime(2026, 1, 31), True),
        (
            "Published: 2026-01-01",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            None,
            True,
        ),
        (
            "Published: 2026-01-31",
            None,
            datetime(2026, 1, 31, tzinfo=timezone.utc),
            True,
        ),
        (
            "Published: 2026-01-01",
            datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 1, 1, 16, tzinfo=timezone(timedelta(hours=-8))),
            True,
        ),
        ("", datetime(2026, 1, 1), None, False),
        ("", None, datetime(2026, 12, 31), False),
        ("Published: August 2026", datetime(2026, 1, 1), None, False),
        ("", None, None, True),
        ("Published: 2020-01-01", None, None, True),
    ],
)
def test_search_applies_publication_date_window(tmp_path, label, start, end, included):
    write_pdf(tmp_path / "paper.pdf", label)
    source = local_pdf.LocalPDFSource(str(tmp_path))

    results = asyncio.run(
        source.search(UserCriteria(topics=["factor"], date_from=start, date_to=end))
    )

    assert len(results) == int(included)
    assert source.last_search.fetched == 1
    assert source.last_search.successful_requests == 1
    assert source.last_search.errors == []


def test_date_rejections_do_not_consume_result_limit_or_ignore_override(tmp_path):
    configured = tmp_path / "configured"
    configured.mkdir()
    selected = tmp_path / "selected"
    selected.mkdir()
    write_pdf(selected / "01-old.pdf", "Published: 2020-01-01")
    write_pdf(selected / "02-current.pdf", "Published: 2026-03-01")
    write_pdf(selected / "03-current.pdf", "Published: 2026-04-01")
    source = local_pdf.LocalPDFSource(str(configured))

    results = asyncio.run(
        source.search(
            UserCriteria(
                topics=["factor"],
                local_pdf_path=str(selected),
                date_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                max_results_per_source=1,
            )
        )
    )

    assert [row.published_date for row in results] == [date(2026, 3, 1)]
    assert source.last_search.fetched == 2
    assert source.last_search.successful_requests == 2
