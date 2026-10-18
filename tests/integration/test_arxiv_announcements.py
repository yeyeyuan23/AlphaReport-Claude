"""Replay real dated arXiv lists captured after Saturday's RSS rollover."""

from reportagent.sources.date_window import source_date


import asyncio
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import httpx
import pytest
from tests.support.source_fixtures import mock_http

from reportagent.models.schemas import UserCriteria
from reportagent.sources import arxiv_announcements as announcements
from reportagent.sources.arxiv_catalog import (
    AnnouncementBatch,
    parse_recent_index,
    parse_batch_page,
    requested_days,
)
from reportagent.sources.arxiv_metadata import read_abstract_page
from reportagent.sources.arxiv_source import ArxivSource
from reportagent.sources.base import SourceParseError, SearchDiagnostics

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "arxiv"


def criteria(**updates):
    return UserCriteria(
        topics=["portfolio"],
        **{
            "date_basis": "announcement",
            "date_from": datetime(2026, 10, 16, 8, tzinfo=timezone(timedelta(hours=8))),
            "date_to": datetime(2026, 10, 17, 8, tzinfo=timezone(timedelta(hours=8))),
            "max_results_per_source": None,
            **updates,
        },
    )


def recorded(name):
    return (FIXTURES / name).read_bytes()


def install_http(monkeypatch, respond):
    mock_http(monkeypatch, respond)


def test_weekend_rerun_reads_complete_previous_public_batch(monkeypatch):
    urls = []

    def respond(request):
        urls.append(str(request.url))
        if request.url.host == "arxiv.org":
            assert request.url.path == "/list/q-fin/recent"
            return httpx.Response(200, content=recorded("q-fin-recent.html"))
        assert len(request.url.params["id_list"].split(",")) == 17
        return httpx.Response(200, content=recorded("q-fin-metadata.xml"))

    install_http(monkeypatch, respond)
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(source.search_broad(criteria()))
    assert len(rows) == 17
    assert all(
        r.published_date == source_date(datetime(2026, 10, 16, tzinfo=timezone.utc))
        for r in rows
    )
    assert all(r.abstract_only and r.abstract for r in rows)
    assert any(r.arxiv_id == "2609.34004v2" for r in rows)
    assert source.last_search.errors == []
    assert source.last_search.fetched == 17
    assert not any("rss.arxiv" in url or "/pdf/" in url for url in urls)


def test_real_index_locates_prior_day_without_scanning_history():
    batches = parse_recent_index(recorded("q-fin-recent.html"), "q-fin")
    assert [(b.day, b.offset, b.count) for b in batches[:2]] == [
        (date(2026, 10, 16), 0, 17),
        (date(2026, 10, 15), 17, 9),
    ]
    rows = parse_batch_page(recorded("q-fin-recent.html"), batches[0], 0, 2000)
    assert len(rows) == 17
    assert sum(kind == "cross" for _, kind in rows) == 5


def test_partial_real_page_cannot_claim_complete_coverage():
    body = recorded("cs-lg-first-page.html")
    batches = parse_recent_index(body, "cs.LG")
    assert batches[0].count == 306
    assert len(parse_batch_page(body, batches[0], 0, 25)) == 25
    with pytest.raises(SourceParseError, match="count/date"):
        parse_batch_page(body, batches[0], 0, 2000)


def test_unavailable_history_is_not_complete_empty(monkeypatch):
    install_http(
        monkeypatch,
        lambda r: httpx.Response(200, content=recorded("q-fin-recent.html")),
    )
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(
        source.search(
            criteria(
                date_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
                date_to=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )
        )
    )
    assert rows == []
    assert "unavailable in recent lists" in str(source.last_search.errors)


def test_weekend_empty_still_checks_actual_list(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=recorded("q-fin-recent.html"))

    install_http(monkeypatch, respond)
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(
        source.search(
            criteria(
                date_from=datetime(2026, 10, 17, tzinfo=timezone.utc),
                date_to=datetime(2026, 10, 18, tzinfo=timezone.utc),
            )
        )
    )
    assert rows == [] and source.last_search.errors == []
    assert len(calls) == source.last_search.successful_requests == 1


def test_date_window_includes_both_endpoints_without_daylight_saving_conversion():
    assert list(requested_days(criteria())) == [date(2026, 10, 16), date(2026, 10, 17)]
    assert list(
        requested_days(criteria(date_from=date(2026, 11, 1), date_to=date(2026, 11, 2)))
    ) == [date(2026, 11, 1), date(2026, 11, 2)]


def test_abstract_fallback_preserves_submission_date_separately():
    class HTTP:
        async def get(self, url):
            assert url == "https://arxiv.org/abs/2610.21867v1"
            return httpx.Response(200, content=recorded("2610.21867v1.html"))

    paper = asyncio.run(read_abstract_page(HTTP(), "2610.21867v1"))
    assert paper.arxiv_id == "2610.21867v1" and paper.abstract_only
    assert paper.published_date == source_date(
        datetime(2026, 10, 15, 13, 39, 41, tzinfo=timezone.utc)
    )
    assert "Q-learning" in paper.abstract


def test_query_failure_uses_official_abstract_page():
    entry = (
        "2610.21867",
        AnnouncementBatch(date(2026, 10, 16), 0, 1),
        "q-fin",
        "new",
        "https://arxiv.org/list/q-fin/recent",
    )

    class HTTP:
        async def get(self, url, **kwargs):
            if "export.arxiv" in url:
                raise httpx.ConnectError("query unavailable")
            return httpx.Response(200, content=recorded("2610.21867v1.html"))

    results, diagnostics = [], SearchDiagnostics()
    asyncio.run(announcements._fetch_abstracts(HTTP(), [entry], results, diagnostics))
    assert len(results) == 1 and diagnostics.errors == []
    assert (
        results[0].raw_metadata["publication_date_info"]["source"]
        == "arxiv_dated_listing"
    )


def test_publication_mode_unlimited_still_pages_submitted_date_api(monkeypatch):
    from tests.support.source_fixtures import atom_entry, atom_feed

    offsets = []

    def respond(request):
        assert request.url.host == "export.arxiv.org"
        assert (
            "submittedDate:[202001010000 TO 202001032359]"
            in request.url.params["search_query"]
        )
        offset = int(request.url.params["start"])
        offsets.append(offset)
        count = 100 if offset == 0 else 3
        return httpx.Response(
            200,
            text=atom_feed(
                "".join(
                    atom_entry(f"2001.{n:05d}v1", published="2020-01-02T04:00:00Z")
                    for n in range(offset, offset + count)
                ),
                103,
            ),
        )

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
    )
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(
        source.search(
            criteria(
                date_basis="publication",
                date_from=datetime(2020, 1, 1),
                date_to=datetime(2020, 1, 3, 23, 59),
            )
        )
    )
    assert len(rows) == 103
    assert offsets == [0, 100]
    assert source.last_search.errors == []
