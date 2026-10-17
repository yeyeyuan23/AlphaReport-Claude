"""Date windows and acquired research text must reflect source evidence."""

import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest


from reportagent.models.schemas import UserCriteria, SourceType
from reportagent.sources.arxiv_source import ArxivSource
from reportagent.sources.bigquant_source import BigQuantSource
from reportagent.sources.eastmoney_source import EastMoneySource
from tests.support.source_fixtures import (
    collect as collection_fixture,
    fake,
    paper,
    mock_http,
    atom_entry,
    atom_feed,
)
from tests.support.collection_scoring import RelevanceModel


# Pytest discovers imported fixtures through module attributes.
collect = collection_fixture


def test_collection_end_date_covers_last_day_in_utc(collect):
    source = fake(SourceType.ARXIV)
    collect([source], date_from="2026-10-05", date_to="2026-10-11")
    criteria = source.search.call_args.args[0]
    assert criteria.date_from == datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert criteria.date_to == datetime(
        2026, 10, 11, 23, 59, 59, 999999, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("broad", [False, True])
def test_arxiv_historical_range_is_sent_to_api_before_result_limit(monkeypatch, broad):
    queries = []

    def respond(request):
        queries.append(request.url.params["search_query"])
        entries = atom_entry("2001.00001v3", published="2020-01-03T23:59:30Z")
        # Without the range the API's newest N are unrelated to the requested week.
        return httpx.Response(
            200,
            text=atom_feed(
                entries
                if "submittedDate:[202001010000 TO 202001032359]" in queries[-1]
                else "",
                1 if "submittedDate:" in queries[-1] else 0,
            ),
        )

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
    )
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    criteria = UserCriteria(
        topics=["portfolio"],
        date_from=datetime(2020, 1, 1),
        date_to=datetime(2020, 1, 3, 23, 59, 59),
        max_results_per_source=1,
    )
    rows = asyncio.run((source.search_broad if broad else source.search)(criteria))
    assert [row.arxiv_id for row in rows] == ["2001.00001v3"]
    assert "updatedDate" not in queries[0]


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_arxiv_original_publication_does_not_change_with_requested_revision(version):
    entry = ElementTree.fromstring(
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + atom_entry("2001.00001" + version, published="2020-01-03T12:00:00Z")
        + "</feed>"
    )[0]
    row = ArxivSource._parse_atom_entry(entry)
    assert row.published_date == datetime(2020, 1, 3, 12, tzinfo=timezone.utc)
    assert row.raw_metadata["updated_date"] == "2026-10-16T00:00:00+00:00"


def test_arxiv_date_filter_converts_offsets_and_excludes_unknown_dates():
    rows = [
        paper(
            published_date=datetime(
                2026, 10, 12, 7, tzinfo=timezone(timedelta(hours=8))
            )
        ),
        paper(published_date=None),
        paper(published_date=datetime(2026, 10, 12, 1, tzinfo=timezone.utc)),
    ]
    selected = ArxivSource()._filter_by_date(
        rows,
        datetime(2026, 10, 11, tzinfo=timezone.utc),
        datetime(2026, 10, 11, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert selected == rows[:1]


def test_bigquant_date_window_applies_before_limit_and_unknown_is_excluded(monkeypatch):
    def respond(request):
        path = request.url.path
        if "/collection/" in path:
            return httpx.Response(
                200,
                text="<h1>Collection</h1>"
                + "".join(
                    f'<a href="/wiki/doc/{name}">portfolio risk model {name}</a>'
                    for name in ["old", "unknown", "in-range"]
                ),
            )
        date = {
            "old": "2020-01-01",
            "unknown": None,
            "in-range": "2026-10-11T23:30:00Z",
        }[path.rsplit("/", 1)[1]]
        meta = f'<meta name="citation_date" content="{date}">' if date else ""
        return httpx.Response(
            200,
            text=meta
            + "<article><h1>Portfolio risk</h1><p>A portfolio model estimates risk using observed market data.</p></article>",
        )

    mock_http(monkeypatch, respond)
    rows = asyncio.run(
        BigQuantSource(rate_limit_seconds=0, max_listing_pages=1).search(
            UserCriteria(
                topics=["portfolio"],
                date_from=datetime(2026, 10, 5),
                date_to=datetime(2026, 10, 11, 23, 59, 59),
                max_results_per_source=1,
            )
        )
    )
    assert [row.source_url for row in rows] == [
        "https://bigquant.com/wiki/doc/in-range"
    ]


def test_failed_llm_classification_retains_report_but_marks_source_partial(
    collect, monkeypatch
):
    from reportagent.services import collection
    from unittest.mock import AsyncMock

    monkeypatch.setattr(collection, "LLMClient", RelevanceModel)
    monkeypatch.setattr(
        collection,
        "LLMClassifier",
        lambda _: type(
            "Classifier",
            (),
            {
                "classify": AsyncMock(
                    side_effect=RuntimeError("Authorization: Bearer should-not-leak")
                )
            },
        )(),
    )
    result, rows = collect([fake(SourceType.ARXIV, [paper()])])
    assert len(rows) == 1
    assert result["status"] == "partial"
    assert "classification" in " ".join(result["source_statuses"][0]["errors"]).lower()
    assert "should-not-leak" not in str(result)


def pdf_bytes(text):
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 550, 800), text, fontsize=11)
    data = document.tobytes()
    document.close()
    return data


def test_bigquant_skips_out_of_window_and_undated_pdfs_before_download(
    tmp_path, monkeypatch
):
    from reportagent.sources import content_fetch

    monkeypatch.setattr(content_fetch, "PDF_DIRECTORY", tmp_path)
    dates = {
        "old": "2020-01-01",
        "unknown": None,
        "future": "2026-10-12",
        "lower": "2026-10-05T00:00:00Z",
        "upper": "2026-10-11T23:59:59Z",
    }
    downloads = []
    attachment = pdf_bytes(
        "A portfolio risk model uses observed returns to estimate risk budgets. " * 8
    )

    def respond(request):
        path = request.url.path
        if "/collection/" in path:
            return httpx.Response(
                200,
                text="<h1>Collection</h1>"
                + "".join(
                    f'<a href="/wiki/doc/{name}">portfolio {name}</a>' for name in dates
                ),
            )
        if path.endswith(".pdf"):
            downloads.append(path)
            return (
                httpx.Response(200, content=attachment)
                if path in {"/pdf/lower.pdf", "/pdf/upper.pdf"}
                else httpx.Response(404)
            )
        name = path.rsplit("/", 1)[1]
        meta = (
            f'<meta name="citation_date" content="{dates[name]}">'
            if dates[name]
            else ""
        )
        return httpx.Response(
            200,
            text=meta + f"<article><h1>Portfolio {name}</h1>"
            f'<a href="/pdf/{name}.pdf">PDF</a></article>',
        )

    mock_http(monkeypatch, respond)
    source = BigQuantSource(rate_limit_seconds=0, max_listing_pages=1)
    rows = asyncio.run(
        source.search(
            UserCriteria(
                topics=["portfolio"],
                date_from=datetime(2026, 10, 5, tzinfo=timezone.utc),
                date_to=datetime(2026, 10, 11, 23, 59, 59, tzinfo=timezone.utc),
                max_results_per_source=2,
            )
        )
    )

    assert downloads == ["/pdf/lower.pdf", "/pdf/upper.pdf"]
    assert [row.title for row in rows] == ["Portfolio lower", "Portfolio upper"]
    assert all(row.full_text and not row.abstract_only for row in rows)
    assert (
        len(list(tmp_path.glob("*.pdf"))) == 1
    )  # Identical PDFs share the content cache.
    assert not source.last_search.errors


@pytest.mark.parametrize("entrypoint", ["search", "hydrate_result"])
def test_bigquant_undated_pdf_is_available_without_a_date_window(
    tmp_path, monkeypatch, entrypoint
):
    from reportagent.sources import content_fetch

    monkeypatch.setattr(content_fetch, "PDF_DIRECTORY", tmp_path)
    attachment = pdf_bytes(
        "Portfolio risk is estimated from daily returns and compared with equal risk budgets. "
        * 8
    )

    def respond(request):
        if "/collection/" in request.url.path:
            return httpx.Response(
                200,
                text='<h1>Collection</h1><a href="/wiki/doc/undated">portfolio undated</a>',
            )
        if request.url.path.endswith(".pdf"):
            return httpx.Response(200, content=attachment)
        return httpx.Response(
            200,
            text='<article><h1>Portfolio undated</h1><a href="/pdf/undated.pdf">PDF</a></article>',
        )

    mock_http(monkeypatch, respond)
    source = BigQuantSource(rate_limit_seconds=0, max_listing_pages=1)
    if entrypoint == "search":
        rows = asyncio.run(source.search(UserCriteria(topics=["portfolio"])))
        assert len(rows) == 1
        row = rows[0]
    else:
        row = asyncio.run(
            source.hydrate_result(
                paper(
                    source=SourceType.BIGQUANT,
                    source_url="https://bigquant.com/wiki/doc/undated",
                )
            )
        )
    assert row.published_date is None
    assert row.full_text and not row.abstract_only
    assert Path(row.pdf_path).is_file()
    assert not source.last_search.errors


def fetch_bigquant(html, attachment=None):
    async def run():
        def respond(request):
            return (
                httpx.Response(200, content=attachment)
                if request.url.path.endswith(".pdf")
                else httpx.Response(200, text=html)
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            source = BigQuantSource(rate_limit_seconds=0)
            row = await source._fetch_detail(client, "Fixture", "/wiki/doc/fixture")
            return row, source.last_search

    return asyncio.run(run())


def test_bigquant_real_html_body_is_full_text_and_placeholder_meta_is_ignored():
    paragraph = "We estimate a portfolio risk model using daily equity returns and evaluate realized portfolio volatility. "
    row, diagnostics = fetch_bigquant(
        '<meta name="description" content="\\..."><article><h1>Portfolio risk</h1>'
        "<div>由author创建，最终由editor<time>更新于2026-10-16</time> 被浏览 10 用户</div>"
        f"<p>{paragraph}</p><p>{paragraph}The second section defines the estimator.</p></article>"
    )
    assert row.full_text and "The second section" in row.full_text
    assert row.abstract_only is False
    assert "We estimate" in row.abstract and "\\..." not in row.abstract
    assert "由author创建" not in row.full_text and "更新于" not in row.full_text
    assert not diagnostics.errors


@pytest.mark.parametrize(
    "fixture", ["placeholder-factor.html", "placeholder-risk-budget.html"]
)
def test_bigquant_placeholder_uses_public_attachment_and_saves_valid_pdf(
    tmp_path, monkeypatch, fixture
):
    from reportagent.sources import content_fetch

    monkeypatch.setattr(content_fetch, "PDF_DIRECTORY", tmp_path)
    text = (
        "Portfolio risk methodology. We estimate financial factors from observed returns and compare risk budgets. "
        * 6
    )
    html = (
        Path(__file__).resolve().parents[1] / "fixtures/bigquant" / fixture
    ).read_text(encoding="utf-8")
    row, diagnostics = fetch_bigquant(html, pdf_bytes(text))
    assert row.full_text and "compare risk budgets" in row.full_text
    assert row.abstract_only is False
    assert row.abstract and row.abstract != "\\..."
    assert Path(row.pdf_path).read_bytes().startswith(b"%PDF-")
    assert row.published_date is None  # No publication evidence in these cached pages.
    assert not diagnostics.errors


def test_bigquant_invalid_attachment_keeps_title_link_without_fake_abstract():
    html = '<meta name="description" content="\\..."><article><h1>Portfolio risk</h1><p>\\</p><a href="/paper.pdf">PDF</a></article>'
    row, diagnostics = fetch_bigquant(html, b"<html>captcha</html>")
    assert row.title == "Portfolio risk" and row.source_url.endswith("/fixture")
    assert row.abstract is None and row.full_text is None and row.abstract_only
    assert diagnostics.errors and "pdf" in " ".join(diagnostics.errors).lower()


def test_eastmoney_metadata_is_not_an_abstract():
    row = EastMoneySource()._parse_item(
        {
            "title": "Portfolio risk",
            "infoCode": "A1",
            "orgSName": "Broker",
            "attachPages": 42,
            "predictThisYearEps": "3.2",
        },
        2,
    )
    assert row.abstract is None and row.full_text is None
    assert (
        row.raw_metadata["org_name"] == "Broker"
        and row.raw_metadata["attach_pages"] == 42
    )


@pytest.mark.parametrize("attachment", [False, True])
def test_eastmoney_fetches_actual_article_or_public_pdf(
    monkeypatch, tmp_path, attachment
):
    from reportagent.sources import content_fetch

    monkeypatch.setattr(content_fetch, "PDF_DIRECTORY", tmp_path)
    text = (
        "A portfolio optimization model estimates covariance from market data. The evidence compares factor risk budgets. "
        * 6
    )

    def respond(request):
        if request.url.path.endswith(".pdf"):
            return httpx.Response(200, content=pdf_bytes(text))
        if "/report/info/" in request.url.path:
            html = (
                '<div class="ctx-content"><a href="https://pdf.dfcfw.com/pdf/H3_A1_1.pdf">Download</a></div>'
                if attachment
                else '<div class="ctx-content"><p>' + text + "</p></div>"
            )
            return httpx.Response(200, text=html)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "title": "Portfolio optimization risk model",
                        "infoCode": "A1",
                        "orgSName": "Broker",
                        "attachPages": 42,
                    }
                ],
                "TotalPage": 1,
            },
        )

    mock_http(monkeypatch, respond)
    source = EastMoneySource(rate_limit_seconds=0)
    monkeypatch.setattr(source, "_resolve_qtypes", lambda _: [2])
    rows = asyncio.run(source.search(UserCriteria(topics=["portfolio"])))
    assert len(rows) == 1
    row = rows[0]
    assert row.full_text and "compares factor risk budgets" in row.full_text
    assert row.abstract and "covariance" in row.abstract and "页数" not in row.abstract
    assert not row.abstract_only
    if attachment:
        assert Path(row.pdf_path).is_file()


def test_download_size_limit_preserves_valid_siblings(monkeypatch, tmp_path):
    from reportagent.sources import content_fetch

    monkeypatch.setattr(content_fetch, "PDF_DIRECTORY", tmp_path)
    monkeypatch.setattr(
        content_fetch,
        "get_config",
        lambda *args, **kwargs: {"max_size_mb": 100 / 1024 / 1024},
    )
    html = '<article><h1>Portfolio risk</h1><p>\\</p><a href="/paper.pdf">PDF</a></article>'
    row, diagnostics = fetch_bigquant(html, b"%PDF-" + b"x" * 101)
    assert row.full_text is None and row.abstract is None
    assert diagnostics.errors and "size" in " ".join(diagnostics.errors).lower()
    assert not list(tmp_path.iterdir())


def test_source_hydration_public_methods_return_evidence_without_models(monkeypatch):
    text = (
        "The portfolio methodology estimates covariance and tests financial risk budgets on observed market returns. "
        * 3
    )
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, text="<article><h1>Portfolio risk</h1><p>" + text + "</p></article>"
        ),
    )
    for adapter, source in [
        (BigQuantSource(), SourceType.BIGQUANT),
        (EastMoneySource(), SourceType.EASTMONEY),
    ]:
        row = asyncio.run(
            adapter.hydrate_result(
                paper(source=source, source_url="https://example.org/report")
            )
        )
        assert row.full_text == text.strip()
        assert row.abstract and "methodology" in row.abstract


def test_bigquant_metadata_only_article_does_not_use_generic_site_description():
    generic = "BigQuant人工智能AI量化交易知识库提供AI量化交易入门知识点、股票期货金融、量化交易策略开发常见问题。"
    row, diagnostics = fetch_bigquant(
        '<meta name="description" content="'
        + generic
        + '">'
        + "<article><h1>Portfolio risk</h1><p>\\...</p></article>"
    )
    assert row.abstract is None and row.full_text is None
    assert diagnostics.errors


def test_arxiv_offset_publication_is_normalized_before_database_storage():
    entry = ElementTree.fromstring(
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + atom_entry("2610.00001v2", published="2026-10-12T07:00:00+08:00")
        + "</feed>"
    )[0]
    row = ArxivSource._parse_atom_entry(entry)
    assert row.published_date.isoformat() == "2026-10-11T23:00:00+00:00"


def test_bigquant_bad_pdf_does_not_remove_valid_sibling(collect, monkeypatch, tmp_path):
    from reportagent.sources import content_fetch

    monkeypatch.setattr(content_fetch, "PDF_DIRECTORY", tmp_path)
    text = (
        "A finance portfolio risk model evaluates covariance estimates and risk budgets on market data. "
        * 5
    )

    def respond(request):
        path = request.url.path
        if "/collection/" in path:
            return httpx.Response(
                200,
                text='<a href="/wiki/doc/bad">portfolio risk bad</a><a href="/wiki/doc/good">portfolio risk good</a>',
            )
        if path.endswith("good.pdf"):
            return httpx.Response(200, content=pdf_bytes(text))
        if path.endswith("bad.pdf"):
            return httpx.Response(200, text="<html>blocked</html>")
        key = path.rsplit("/", 1)[1]
        return httpx.Response(
            200,
            text=f'<article><h1>Portfolio optimization quantitative risk model {key}</h1><p>\\...</p><a href="/{key}.pdf">PDF</a></article>',
        )

    mock_http(monkeypatch, respond)
    result, rows = collect([BigQuantSource(rate_limit_seconds=0, max_listing_pages=1)])
    assert len(rows) == 1
    good = next(row for row in rows if row.title.endswith("good"))
    assert good.full_text and good.pdf_path
    assert all(not row.title.endswith("bad") for row in rows)
    assert result["source_statuses"][0]["screening"]["pending"] == 1
    assert (
        result["status"] == "partial" and result["source_statuses"][0]["full_text"] == 1
    )


def test_arxiv_pdf_stream_size_is_bounded_and_abstract_survives(collect, monkeypatch):
    from reportagent.sources import content_fetch

    monkeypatch.setattr(
        content_fetch,
        "get_config",
        lambda *args, **kwargs: {"max_size_mb": 100 / 1024 / 1024},
    )
    source = ArxivSource(rate_limit_seconds=0)
    monkeypatch.setattr(
        source,
        "_execute_search",
        lambda *args, **kwargs: [
            paper(
                arxiv_id="2610.00001v1",
                raw_metadata={"pdf_url": "https://example.org/paper.pdf"},
            )
        ],
    )
    mock_http(
        monkeypatch, lambda request: httpx.Response(200, content=b"%PDF-" + b"x" * 101)
    )
    monkeypatch.setattr(
        source,
        "_save_pdf_bytes",
        lambda *args: pytest.fail("oversized response must not reach disk"),
    )
    result, rows = collect([source])
    assert len(rows) == 1 and rows[0].abstract == "A finance portfolio risk model"
    assert rows[0].full_text is None
    assert result["status"] == "partial"
    assert "size" in " ".join(result["source_statuses"][0]["errors"]).lower()


def test_bigquant_html_preview_does_not_prevent_fetching_linked_full_pdf(
    tmp_path, monkeypatch
):
    from reportagent.sources import content_fetch

    monkeypatch.setattr(content_fetch, "PDF_DIRECTORY", tmp_path)
    preview = (
        "This website previews the portfolio method and provides a link to the underlying research report. "
        * 15
    )
    document = (
        "The full report describes the covariance estimator and its complete financial evaluation protocol. "
        * 10
    )
    row, diagnostics = fetch_bigquant(
        "<article><h1>Portfolio risk</h1><p>"
        + preview
        + '</p><a href="/full-report.pdf">Original PDF</a></article>',
        pdf_bytes(document),
    )
    assert row.full_text and "complete financial evaluation protocol" in row.full_text
    assert row.pdf_path and Path(row.pdf_path).is_file()
    assert row.raw_metadata["content_kind"] == "pdf"
    assert row.raw_metadata["text_scope"] == "linked_pdf"
    assert not diagnostics.errors


def test_bigquant_failed_pdf_keeps_html_evidence_with_explicit_scope():
    preview = (
        "The article describes the portfolio covariance estimator and reports an empirical comparison of risk budgets. "
        * 4
    )
    row, diagnostics = fetch_bigquant(
        "<article><h1>Portfolio risk</h1><p>"
        + preview
        + '</p><a href="/unavailable.pdf">Original PDF</a></article>',
        b"<html>not a PDF</html>",
    )
    assert row.full_text == preview.strip() and row.abstract
    assert row.pdf_path is None
    assert row.raw_metadata["content_kind"] == "html"
    assert row.raw_metadata["text_scope"] == "html_article"
    assert (
        row.abstract_only is True
    )  # A failed report PDF leaves only its HTML preview.
    assert diagnostics.errors
