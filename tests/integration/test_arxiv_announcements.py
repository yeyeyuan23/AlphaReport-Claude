"""Announcement dates must come from RSS evidence rather than first submission."""

import asyncio
from datetime import datetime, timezone, timedelta

import httpx
import pytest

from reportagent.models.schemas import UserCriteria
from reportagent.sources.arxiv_source import ArxivSource
from tests.support.arxiv_announcement_fixtures import BATCH, rss_feed, rss_item


EXPECTED_CATEGORIES = {
    "q-fin.CP",
    "q-fin.EC",
    "q-fin.GN",
    "q-fin.MF",
    "q-fin.PM",
    "q-fin.PR",
    "q-fin.RM",
    "q-fin.ST",
    "q-fin.TR",
    "cs.LG",
    "stat.ML",
    "cs.CL",
    "cs.AI",
    "math.OC",
}


def criteria(**updates):
    return UserCriteria(
        topics=["portfolio"],
        **{
            "date_basis": "announcement",
            "date_from": datetime(2026, 10, 16, 8, tzinfo=timezone(timedelta(hours=8))),
            "date_to": datetime(2026, 10, 17, 8, tzinfo=timezone(timedelta(hours=8))),
            "date_to_exclusive": True,
            "max_results_per_source": None,
            **updates,
        },
    )


def install_http(monkeypatch, respond):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
    )


def search(monkeypatch, items, *, broad=True, settings=None):
    install_http(monkeypatch, lambda request: httpx.Response(200, text=rss_feed(items)))
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(
        (source.search_broad if broad else source.search)(settings or criteria())
    )
    return rows, source.last_search


@pytest.mark.parametrize("broad", [False, True])
def test_all_announcements_survive_without_top15_cap_and_duplicate_categories(
    monkeypatch, broad
):
    rows, diagnostics = search(
        monkeypatch,
        "".join(rss_item(f"2610.{n:05d}v1") for n in range(1, 26)),
        broad=broad,
    )
    assert len(rows) == 25
    assert diagnostics.fetched == 350
    assert diagnostics.successful_requests == 14
    assert diagnostics.errors == []


def test_announcement_uses_item_pubdate_and_preserves_version_without_inventing_original_date(
    monkeypatch,
):
    rows, _ = search(monkeypatch, rss_item("2510.00001v1"))
    row = rows[0]
    assert row.announced_at == datetime(2026, 10, 16, 4, tzinfo=timezone.utc)
    assert row.published_date is None
    assert row.arxiv_id == "2510.00001v1"
    assert row.authors == ["Alice Author", "Bob Writer"]
    assert row.abstract == "We estimate financial market risk from returns."
    assert row.raw_metadata["pdf_url"] == "https://arxiv.org/pdf/2510.00001v1"
    assert row.raw_metadata["announcement_info"]["source"] == "arxiv_rss"
    assert row.raw_metadata["announcement_info"]["pubDate"] == BATCH
    assert row.raw_metadata["announcement_info"]["feed_url"].startswith(
        "https://rss.arxiv.org/rss/"
    )


def test_half_open_beijing_window_and_default_update_exclusion(monkeypatch):
    items = "".join(
        [
            rss_item("2610.00001v1", stamp="Fri, 16 Oct 2026 00:00:00 +0000"),
            rss_item("2610.00002v1", stamp="Sat, 17 Oct 2026 00:00:00 +0000"),
            rss_item("2610.00003v1", stamp="Thu, 15 Oct 2026 23:59:59 +0000"),
            rss_item("2510.00004v2", announce_type="replace"),
            rss_item("2510.00005v2", announce_type="replace-cross"),
            rss_item("2510.00006v1", announce_type="cross"),
        ]
    )
    rows, _ = search(monkeypatch, items)
    assert [row.arxiv_id for row in rows] == ["2610.00001v1", "2510.00006v1"]


def test_each_category_is_independent_and_metadata_mode_does_not_download(monkeypatch):
    requested = []

    def respond(request):
        requested.append(str(request.url))
        assert request.url.host == "rss.arxiv.org"
        category = request.url.path.removeprefix("/rss/")
        index = sorted(EXPECTED_CATEGORIES).index(category)
        return httpx.Response(200, text=rss_feed(rss_item(f"2610.{index:05d}v1")))

    install_http(monkeypatch, respond)
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(source.search(criteria()))
    assert len(rows) == 14
    assert {url.rsplit("/", 1)[1] for url in requested} == EXPECTED_CATEGORIES
    assert len(requested) == 14


@pytest.mark.parametrize(
    "bad",
    [
        "<html>secret</html>",
        "<rss><channel>",
        rss_feed(rss_item(stamp="invalid")),
        rss_feed(rss_item().replace("<pubDate>" + BATCH + "</pubDate>", "")),
    ],
)
def test_malformed_feed_or_item_preserves_other_categories_and_marks_incomplete(
    monkeypatch, bad
):
    def respond(request):
        return httpx.Response(
            200,
            text=bad if request.url.path.endswith("q-fin.RM") else rss_feed(rss_item()),
        )

    install_http(monkeypatch, respond)
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(source.search_broad(criteria()))
    assert len(rows) == 1
    assert source.last_search.errors
    assert "secret" not in str(source.last_search.errors)
    assert source.last_search.successful_requests >= 13


def test_network_failure_preserves_other_categories_and_never_falls_back_to_submission(
    monkeypatch,
):
    def respond(request):
        assert request.url.host == "rss.arxiv.org"
        if request.url.path.endswith("cs.LG"):
            raise httpx.ConnectError(
                "https://example.test?token=do-not-log", request=request
            )
        return httpx.Response(200, text=rss_feed(rss_item()))

    install_http(monkeypatch, respond)
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    rows = asyncio.run(source.search(criteria()))
    assert len(rows) == 1
    assert source.last_search.successful_requests == 13
    assert "ConnectError" in str(source.last_search.errors)
    assert "do-not-log" not in str(source.last_search.errors)


@pytest.mark.parametrize(
    "batch", ["Thu, 15 Oct 2026 04:00:00 +0000", "Sat, 17 Oct 2026 04:00:00 +0000"]
)
def test_latest_batch_outside_window_is_incomplete_even_when_lastbuild_is_current(
    monkeypatch, batch
):
    install_http(
        monkeypatch, lambda request: httpx.Response(200, text=rss_feed(batch=batch))
    )
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    assert asyncio.run(source.search(criteria())) == []
    assert source.last_search.errors
    assert "latest batch" in str(source.last_search.errors).lower()


def test_wide_historical_window_is_explicitly_incomplete(monkeypatch):
    rows, diagnostics = search(
        monkeypatch,
        rss_item(),
        settings=criteria(date_from=datetime(2026, 10, 1, tzinfo=timezone.utc)),
    )
    assert len(rows) == 1
    assert "latest batch" in str(diagnostics.errors).lower()


def test_2000_entry_feed_records_possible_cutoff_without_dropping_valid_items(
    monkeypatch,
):
    items = "".join(rss_item(f"2610.{n:05d}v1") for n in range(2000))
    rows, diagnostics = search(monkeypatch, items)
    assert len(rows) == 2000
    assert "2000" in str(diagnostics.errors)


def test_duplicate_within_feed_is_reported_but_cross_category_duplicates_are_normal(
    monkeypatch,
):
    rows, diagnostics = search(monkeypatch, rss_item() + rss_item())
    assert len(rows) == 1
    assert "repeated" in str(diagnostics.errors).lower()


def test_valid_empty_current_feed_is_complete_empty(monkeypatch):
    rows, diagnostics = search(monkeypatch, "")
    assert rows == []
    assert diagnostics.errors == []
    assert diagnostics.successful_requests == 14


def test_explicit_finite_limit_is_observable_as_incomplete(monkeypatch):
    rows, diagnostics = search(
        monkeypatch,
        "".join(rss_item(f"2610.{n:05d}v1") for n in range(4)),
        settings=criteria(max_results_per_source=2),
    )
    assert len(rows) == 2
    assert "limit" in str(diagnostics.errors)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.replace("2510.00001v1 Announce", "2510.00001 Announce"),
        lambda item: item.replace(
            "oai:arXiv.org:2510.00001v1", "oai:arXiv.org:2510.99999v1"
        ),
        lambda item: item.replace(
            "https://arxiv.org/abs/2510.00001", "https://untrusted.test/abs/2510.00001"
        ),
        lambda item: item.replace("<arxiv:announce_type>new</arxiv:announce_type>", ""),
        lambda item: item.replace("Announce Type: new", "Announce Type: replace"),
        lambda item: item.replace(
            "Abstract: We estimate financial market risk from returns.", ""
        ),
    ],
)
def test_malformed_identity_or_announcement_metadata_does_not_hide_valid_sibling(
    monkeypatch, mutate
):
    rows, diagnostics = search(
        monkeypatch, mutate(rss_item()) + rss_item("2610.00002v1")
    )
    assert [row.arxiv_id for row in rows] == ["2610.00002v1"]
    assert diagnostics.errors


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


def test_current_channel_with_only_stale_items_cannot_claim_complete_empty(monkeypatch):
    rows, diagnostics = search(
        monkeypatch, rss_item(stamp="Thu, 15 Oct 2026 04:00:00 +0000")
    )
    assert rows == []
    assert "batch" in str(diagnostics.errors).lower()


def test_all_failed_feeds_have_no_success_and_only_safe_diagnostics(monkeypatch):
    install_http(
        monkeypatch,
        lambda request: httpx.Response(503, text="private-token-do-not-log"),
    )
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    assert asyncio.run(source.search_broad(criteria())) == []
    assert source.last_search.successful_requests == 0
    assert len(source.last_search.errors) == 14
    assert "HTTP 503" in str(source.last_search.errors)
    assert "private-token-do-not-log" not in str(source.last_search.errors)


def test_announcement_categories_have_no_aggregate_timeout(monkeypatch):
    # Each category can be slow but successful. A blanket wait_for around all
    # categories would abort later categories despite their individual budgets.
    # Reject that blanket wrapper directly without making the test wait minutes.
    import reportagent.sources.arxiv_source as arxiv_module

    async def reject_aggregate_timeout(awaitable, timeout):
        awaitable.close()
        raise TimeoutError("aggregate RSS timeout would omit remaining categories")

    monkeypatch.setattr(arxiv_module.asyncio, "wait_for", reject_aggregate_timeout)
    rows, diagnostics = search(monkeypatch, rss_item())
    assert len(rows) == 1
    assert diagnostics.successful_requests == 14
    assert diagnostics.errors == []


def test_announcement_cancellation_propagates_after_a_successful_category(monkeypatch):
    requested = []

    def respond(request):
        requested.append(request.url.path)
        if len(requested) == 2:
            raise asyncio.CancelledError
        return httpx.Response(200, text=rss_feed(rss_item()))

    install_http(monkeypatch, respond)
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(source.search(criteria()))
    assert source.last_search.successful_requests == 1
    assert source.last_search.errors == []
    assert len(requested) == 2


@pytest.mark.parametrize(
    "text",
    [
        "We study risk when $0<p<1$ and prove that the portfolio is stable.",
        "Bounds $p<i>q$ and $x<y$ leave the complete conclusion intact.",
        "Display bounds $$p<i>q$$ leave the complete conclusion intact.",
        r"We compare \(0<p<1\) and \[x<y\] with the original model.",
        r"We compare \(p<i>q\) and \[x<b>y\] with the original model.",
        "Literal &lt; and &notin; stay literal after XML decoding; x<y.",
    ],
)
def test_plain_rss_text_preserves_math_and_xml_decoded_content(monkeypatch, text):
    rows, diagnostics = search(monkeypatch, rss_item(title=text, abstract=text))
    assert len(rows) == 1
    assert rows[0].title == text
    assert rows[0].abstract == text
    assert diagnostics.errors == []


def test_encoded_html_wrappers_preserve_math_and_paragraph_boundaries(monkeypatch):
    # rss_item XML-escapes this HTML, like an RSS description with encoded markup.
    abstract = (
        '<p class="summary">Risk &amp; return when $0<p<1$.</p>'
        "<div>With $p<i>q$ and <em>complete</em> evidence.<br>Still intact.</div>"
    )
    rows, diagnostics = search(monkeypatch, rss_item(abstract=abstract))
    assert rows[0].abstract == (
        "Risk & return when $0<p<1$. With $p<i>q$ and complete evidence. Still intact."
    )
    assert diagnostics.errors == []


def test_encoded_html_entities_are_decoded_after_markup_removal(monkeypatch):
    rows, diagnostics = search(
        monkeypatch,
        rss_item(abstract="<p>We prove $0&lt;p&lt;1$ &amp; retain the conclusion.</p>"),
    )
    assert rows[0].abstract == "We prove $0<p<1$ & retain the conclusion."
    assert diagnostics.errors == []


def test_encoded_html_wrappers_leave_plain_comparisons_and_unknown_tags(monkeypatch):
    rows, diagnostics = search(
        monkeypatch,
        rss_item(
            abstract='<p>For x<y, <a href="https://arxiv.org/?a=1&amp;b=2">'
            "the result</a> keeps <unknown> notation.</p>Tail remains."
        ),
    )
    assert rows[0].abstract == (
        "For x<y, the result keeps <unknown> notation. Tail remains."
    )
    assert diagnostics.errors == []
