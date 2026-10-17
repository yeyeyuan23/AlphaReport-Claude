"""Offline source-owned announcement windows and complete metadata pagination."""

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from tests.support.source_fixtures import (
    bigquant_info,
    bigquant_document,
    bigquant_page,
)

from reportagent.models.schemas import UserCriteria
from reportagent.sources.bigquant_source import BigQuantSource
from reportagent.sources.eastmoney_source import EastMoneySource


START = datetime(2026, 10, 15, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 16, 0, tzinfo=timezone.utc)


def criteria(**updates):
    # model_copy also keeps this test usable while shared schema changes land.
    return UserCriteria(topics=["portfolio"]).model_copy(
        update={
            "date_from": START,
            "date_to": END,
            "date_basis": "announcement",
            "date_to_exclusive": True,
            "max_results_per_source": None,
            **updates,
        }
    )


def mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


def em_item(code, stamp="2026-10-15 09:30:00"):
    return {
        "title": "portfolio optimization risk model",
        "infoCode": code,
        "publishDate": stamp,
    }


def article(stamp=None, original="2020年1月2日"):
    meta = (
        f'<meta property="article:published_time" content="{stamp}">' if stamp else ""
    )
    return (
        meta + "<article><h1>portfolio model</h1>"
        f"<p>原报告发布日期：{original}</p><p>{'Portfolio covariance and risk evidence. ' * 5}</p>"
        '<a href="/report.pdf">Paper</a></article>'
    )


def test_eastmoney_unlimited_follows_reported_pages_beyond_800(monkeypatch):
    requests = []

    def respond(request):
        if "/report/list" in request.url.path:
            page = int(request.url.params["pageNo"])
            requests.append(page)
            return httpx.Response(
                200,
                json={
                    "data": [em_item(f"{page}-{n}") for n in range(100)],
                    "TotalPage": 9,
                },
            )
        assert not request.url.path.endswith(".pdf")
        return httpx.Response(
            200, text="<article>Portfolio covariance evidence.</article>"
        )

    mock_http(monkeypatch, respond)
    source = EastMoneySource(rate_limit_seconds=0, download_pdfs=False)
    monkeypatch.setattr(source, "_resolve_qtypes", lambda _: [2])
    rows = asyncio.run(source.search(criteria()))
    assert len(rows) == 900
    assert requests == list(range(1, 10))
    assert source.last_search.errors == []


def test_eastmoney_announcement_window_preserves_intraday_timezone(monkeypatch):
    def respond(request):
        if "/report/list" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "data": [
                        em_item("before", "2026-10-15 07:59:59"),
                        em_item("start", "2026-10-15 08:00:00"),
                        em_item("middle", "2026-10-16 07:59:59"),
                        em_item("end", "2026-10-16 08:00:00"),
                    ],
                    "TotalPage": 1,
                },
            )
        return httpx.Response(200, text="<article>Portfolio evidence.</article>")

    mock_http(monkeypatch, respond)
    source = EastMoneySource(rate_limit_seconds=0, download_pdfs=False)
    monkeypatch.setattr(source, "_resolve_qtypes", lambda _: [2])
    rows = asyncio.run(source.search(criteria()))
    assert [r.raw_metadata["info_code"] for r in rows] == ["start", "middle"]
    assert rows[0].announced_at == START
    assert rows[0].published_date == datetime(2026, 10, 15)
    assert rows[0].raw_metadata["announcement_info"]["source"] == "publishDate"


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"data": [em_item("same")], "TotalPage": 3}, "progress"),
        ({"data": [], "TotalPage": 3}, "empty"),
        ({"data": [], "TotalPage": "3"}, "pagination"),
        ({"data": []}, "pagination"),
    ],
)
def test_eastmoney_unlimited_incomplete_pagination_is_diagnostic(payload, reason):
    async def run():
        source = EastMoneySource(rate_limit_seconds=0, download_pdfs=False)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        ) as client:
            rows = await source._fetch_reports(
                client, 2, "2026-10-15", "2026-10-16", None
            )
        return source, rows

    source, rows = asyncio.run(run())
    assert any(reason in error.lower() for error in source.last_search.errors)
    assert len(rows) <= 1


def test_bigquant_unlimited_processes_all_reported_pages_beyond_15(monkeypatch):
    pages = []

    def respond(request):
        if request.url.path.endswith("collections.info"):
            return bigquant_info()
        offset = json.loads(request.content)["offset"]
        page = offset // 2 + 1
        pages.append(page)
        if page == 13:
            return bigquant_page([])
        return bigquant_page(
            [bigquant_document(f"{page}-{n}", original="2020-01-02") for n in range(2)],
            next_offset=offset + 2,
        )

    mock_http(monkeypatch, respond)
    source = BigQuantSource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(source.search(criteria()))
    assert len(rows) == 24
    assert pages == list(range(1, 14))
    assert rows[0].published_date == datetime(2020, 1, 2)
    assert rows[0].announced_at == datetime(2026, 10, 15, 1, tzinfo=timezone.utc)
    assert (
        rows[0].raw_metadata["announcement_info"]["source"]
        == "bigquant_api:publishedAt"
    )
    assert source.last_search.errors == []


def test_bigquant_announcement_dates_are_independent_of_original_dates(monkeypatch):
    rows = [
        bigquant_document("end", published="2026-10-16T08:00:00+08:00"),
        bigquant_document("start", published="2026-10-15T08:00:00+08:00"),
        bigquant_document("before", published="2026-10-15T07:59:59+08:00"),
        bigquant_document("unknown", published=None),
    ]
    mock_http(
        monkeypatch,
        lambda request: bigquant_info()
        if request.url.path.endswith("collections.info")
        else bigquant_page(rows),
    )
    source = BigQuantSource(rate_limit_seconds=0, download_pdfs=False)
    results = asyncio.run(source.search(criteria()))
    assert [r.source_url.rsplit("/", 1)[1] for r in results] == ["start"]
    assert any(
        "announcement date unavailable" in error for error in source.last_search.errors
    )


@pytest.mark.parametrize("mode", ["repeated", "failure"])
def test_bigquant_unlimited_keeps_partial_candidates_on_listing_failure(
    monkeypatch, mode
):
    def respond(request):
        if request.url.path.endswith("collections.info"):
            return bigquant_info()
        offset = json.loads(request.content)["offset"]
        if offset and mode == "failure":
            return httpx.Response(503)
        return bigquant_page([bigquant_document("one")], next_offset=offset + 1)

    mock_http(monkeypatch, respond)
    source = BigQuantSource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(source.search(criteria()))
    assert len(rows) == 1
    assert source.last_search.errors


@pytest.mark.parametrize(
    "meta",
    [
        '<meta name="citation_date" content="2026-10-15">',
        '<meta property="article:modified_time" content="2026-10-15T10:00:00+08:00">',
        '<script type="application/ld+json">{"@type":"Comment","datePublished":"2026-10-15"}</script>',
        '<script type="application/ld+json">{"@type":"Article","url":"https://elsewhere/report","datePublished":"2026-10-15"}</script>',
    ],
)
def test_bigquant_announcement_never_uses_citations_modification_or_foreign_objects(
    meta,
):
    async def run():
        source = BigQuantSource(download_pdfs=False)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text=meta + article())
            )
        ) as client:
            row = await source._fetch_detail(
                client,
                "portfolio",
                "/wiki/doc/one",
                date_from=START,
                date_to=END,
                date_basis="announcement",
                date_to_exclusive=True,
            )
        return row, source

    row, source = asyncio.run(run())
    assert row is None
    assert any(
        "announcement date unavailable" in error for error in source.last_search.errors
    )


def test_bigquant_announcement_accepts_own_jsonld_datetime():
    meta = '<script type="application/ld+json">{"@type":"Article","@id":"https://bigquant.com/wiki/doc/one#article","datePublished":"2026-10-15 09:30:00"}</script>'

    async def run():
        source = BigQuantSource(download_pdfs=False)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text=meta + article())
            )
        ) as client:
            return await source._fetch_detail(
                client,
                "portfolio",
                "/wiki/doc/one",
                date_from=START,
                date_to=END,
                date_basis="announcement",
                date_to_exclusive=True,
            )

    row = asyncio.run(run())
    assert row.announced_at == datetime(2026, 10, 15, 1, 30, tzinfo=timezone.utc)
    assert row.published_date == datetime(2020, 1, 2)


def test_bigquant_announcement_does_not_borrow_foreign_microdata_date():
    body = article() + (
        '<article itemscope itemtype="https://schema.org/Article" '
        'itemid="https://another-site.example/original-report">'
        '<time itemprop="datePublished" datetime="2026-10-15T10:00:00+08:00"></time>'
        "</article>"
    )
    # A nested cited Article is part of the HTML evidence, not this page's clock.
    body = (
        body.replace("</article><article itemscope", "<article itemscope")
        + "</article>"
    )

    async def run():
        source = BigQuantSource(download_pdfs=False)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
        ) as client:
            row = await source._fetch_detail(
                client,
                "portfolio",
                "/wiki/doc/one",
                date_from=START,
                date_to=END,
                date_basis="announcement",
                date_to_exclusive=True,
            )
        return row, source

    row, source = asyncio.run(run())
    assert row is None
    assert any(
        "announcement date unavailable" in error for error in source.last_search.errors
    )


def test_eastmoney_date_only_publication_is_not_an_intraday_announcement(monkeypatch):
    detail_requests = []

    def respond(request):
        if "/report/list" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "data": [em_item("day-only", "2026-10-16")],
                    "TotalPage": 1,
                },
            )
        detail_requests.append(request.url.path)
        return httpx.Response(200, text="<article>Portfolio evidence.</article>")

    mock_http(monkeypatch, respond)
    source = EastMoneySource(rate_limit_seconds=0, download_pdfs=False)
    monkeypatch.setattr(source, "_resolve_qtypes", lambda _: [2])
    rows = asyncio.run(source.search(criteria()))
    assert rows == []
    assert detail_requests == []
    assert any(
        "announcement date unavailable" in error for error in source.last_search.errors
    )
    original = source._parse_item(em_item("day-only", "2026-10-16"), 2)
    assert original.published_date == datetime(2026, 10, 16)
    assert original.announced_at is None


@pytest.mark.parametrize(
    "metadata",
    [
        '<meta property="article:published_time" content="2026-10-16">',
        '<script type="application/ld+json">{"@type":"Article","datePublished":"2026年10月16日"}</script>',
        '<meta name="datePublished" content="2026-10-16">',
    ],
)
def test_bigquant_date_only_page_date_is_not_an_intraday_announcement(metadata):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        assert not request.url.path.endswith(".pdf")
        return httpx.Response(200, text=metadata + article())

    async def run():
        source = BigQuantSource(download_pdfs=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            row = await source._fetch_detail(
                client,
                "portfolio",
                "/wiki/doc/one",
                date_from=START,
                date_to=END,
                date_basis="announcement",
                date_to_exclusive=True,
            )
            original = await source._fetch_detail(client, "portfolio", "/wiki/doc/one")
        return row, original, source

    row, original, source = asyncio.run(run())
    assert row is None
    assert original.published_date == datetime(2020, 1, 2)
    assert original.announced_at is None
    assert requests == ["/wiki/doc/one", "/wiki/doc/one"]
    assert any(
        "announcement date unavailable" in error for error in source.last_search.errors
    )


def test_eastmoney_announcement_scans_all_types_and_retains_generic_titles(monkeypatch):
    qtypes = []
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if "/report/list" in request.url.path:
            qtype = int(request.url.params["qType"])
            qtypes.append(qtype)
            item = em_item(f"generic-{qtype}")
            item["title"] = f"A new perspective {qtype}"
            return httpx.Response(200, json={"data": [item], "TotalPage": 1})
        assert not request.url.path.endswith(".pdf")
        return httpx.Response(
            200,
            text=(
                "<article><h1>A new perspective</h1><p>"
                + "A portfolio covariance model tests financial risk budgets. " * 5
                + '</p><a href="/report.pdf">Full report</a></article>'
            ),
        )

    mock_http(monkeypatch, respond)
    source = EastMoneySource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(source.search(criteria()))
    assert qtypes == [0, 1, 2]
    assert len(rows) == 3
    assert all("portfolio covariance model" in row.abstract for row in rows)
    assert not any(path.endswith(".pdf") for path in requests)
    assert source.last_search.errors == []


def test_bigquant_announcement_retains_generic_titles_in_listing_order(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith("collections.info"):
            return bigquant_info()
        return bigquant_page(
            [
                bigquant_document("generic", title="A new perspective"),
                bigquant_document("matched", title="Portfolio risk"),
            ]
        )

    mock_http(monkeypatch, respond)
    source = BigQuantSource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(source.search(criteria()))
    assert [r.source_url.rsplit("/", 1)[1] for r in rows] == ["generic", "matched"]
    assert all("Portfolio covariance and risk evidence" in r.abstract for r in rows)
    assert not any(path.endswith(".pdf") for path in requests)
    assert source.last_search.errors == []
