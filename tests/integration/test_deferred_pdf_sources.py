"""Metadata discovery and explicit full-document reads remain separate offline."""
import asyncio
from pathlib import Path

import httpx
import pytest

from reportagent.models.schemas import SearchResult, SourceType, UserCriteria
from reportagent.sources import content_fetch
from reportagent.sources.arxiv_source import ArxivSource
from reportagent.sources.bigquant_source import BigQuantSource
from reportagent.sources.eastmoney_source import EastMoneySource


PREVIEW = "Visible HTML evidence describes portfolio risk, observed equity returns and covariance estimation. " * 4
PDF_TEXT = "Complete PDF evidence compares portfolio allocations and their realized risk across market periods. " * 6


@pytest.fixture
def offline(monkeypatch, tmp_path):
    import fitz
    from reportagent.utils import config

    with fitz.open() as document:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(40, 40, 550, 800), PDF_TEXT)
        attachment = document.tobytes()
    monkeypatch.setattr(content_fetch, "PDF_DIRECTORY", tmp_path / "pdfs")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(content_fetch, "get_pdf_policy", lambda: content_fetch.PDFPolicy(
        max_attempts=2, retry_backoff_seconds=0))
    requests = []
    state = {"detail_available": True, "first_pdf_status": 404}

    def respond(request):
        requests.append(request)
        path = request.url.path
        if path.endswith(".pdf") or path.startswith("/pdf/2610."):
            if path.endswith("first.pdf"):
                return httpx.Response(state["first_pdf_status"], content=attachment)
            return httpx.Response(200, content=attachment)
        if "/api/query" in path:
            return httpx.Response(200, text='''<feed xmlns="http://www.w3.org/2005/Atom"
                xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
                <opensearch:totalResults>1</opensearch:totalResults><entry>
                <id>https://arxiv.org/abs/2610.00001v2</id><title>Portfolio risk</title>
                <published>2026-10-10T00:00:00Z</published><updated>2026-10-11T00:00:00Z</updated>
                <summary>Abstract about portfolio risk and covariance estimation.</summary>
                <link title="pdf" href="https://arxiv.org/pdf/2610.00001v2" />
                </entry></feed>''')
        if "/report/list" in path:
            return httpx.Response(200, json={"data": [{"title": "Portfolio risk model",
                "infoCode": "A1", "publishDate": "2026-10-10"}], "TotalPage": 1})
        if "/collection/" in path:
            return httpx.Response(200, text='<h1>Collection</h1><a href="/wiki/doc/fixture">Portfolio risk</a>')
        if not state["detail_available"]:
            return httpx.Response(404)
        return httpx.Response(200, text='<meta name="citation_date" content="2026-10-10">'
            '<article class="ctx-content"><h1>Portfolio risk</h1><p>' + PREVIEW + '</p>'
            '<a href="/first.pdf">Report A</a><a href="/second.pdf">Report B</a></article>')

    async_client, sync_client = httpx.AsyncClient, httpx.Client
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_client(
        transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: sync_client(
        transport=httpx.MockTransport(respond), **kwargs))
    return requests, state, tmp_path


def public_source(kind, monkeypatch, **kwargs):
    if kind == "bigquant":
        return BigQuantSource(rate_limit_seconds=0, max_listing_pages=1, **kwargs)
    source = EastMoneySource(rate_limit_seconds=0, **kwargs)
    monkeypatch.setattr(source, "_resolve_qtypes", lambda _: [2])
    return source


def is_pdf(request):
    return request.url.path.endswith(".pdf") or request.url.path.startswith("/pdf/2610.")


@pytest.mark.parametrize("kind", ["eastmoney", "bigquant"])
def test_metadata_search_defers_all_pdf_attachments(offline, monkeypatch, kind):
    requests, _, directory = offline
    source = public_source(kind, monkeypatch, download_pdfs=False)
    rows = asyncio.run(source.search(UserCriteria(topics=["portfolio"])))
    assert len(rows) == 1
    result = rows[0]
    assert not any(is_pdf(request) for request in requests)
    assert not list(directory.rglob("*.pdf")) and result.pdf_path is None
    assert result.abstract_only is True
    assert result.full_text == PREVIEW.strip()  # Keep visible, scoped HTML evidence.
    assert result.raw_metadata["text_scope"] == "html_article"
    assert result.raw_metadata["content_kind"] == "html"
    origin = "https://bigquant.com" if kind == "bigquant" else "https://data.eastmoney.com"
    assert result.raw_metadata["pdf_links"] == [origin + "/first.pdf", origin + "/second.pdf"]


@pytest.mark.parametrize("kind", ["eastmoney", "bigquant"])
def test_explicit_hydration_uses_saved_attachment_urls_and_ignores_search_flag(offline, monkeypatch, kind):
    requests, state, _ = offline
    source = public_source(kind, monkeypatch, download_pdfs=False)
    result = asyncio.run(source.search(UserCriteria(topics=["portfolio"])))[0]
    requests.clear()
    state["detail_available"] = False
    hydrated = asyncio.run(source.hydrate_result(result))
    assert [request.url.path for request in requests] == ["/first.pdf", "/second.pdf"]
    assert not hydrated.abstract_only and "Complete PDF evidence" in hydrated.full_text
    assert Path(hydrated.pdf_path).is_file()
    assert hydrated.raw_metadata["text_scope"] == "linked_pdf"
    assert hydrated.raw_metadata["pdf_links"] == result.raw_metadata["pdf_links"]


@pytest.mark.parametrize("kind", ["eastmoney", "bigquant"])
def test_default_search_still_downloads_attached_pdf(offline, monkeypatch, kind):
    requests, _, _ = offline
    source = public_source(kind, monkeypatch)
    result = asyncio.run(source.search(UserCriteria(topics=["portfolio"])))[0]
    assert source.download_pdfs is True
    assert any(is_pdf(request) for request in requests)
    assert not result.abstract_only and Path(result.pdf_path).is_file()


@pytest.mark.parametrize("kind", ["eastmoney", "bigquant"])
def test_explicit_read_without_saved_links_discovers_and_downloads_pdf(offline, monkeypatch, kind):
    requests, _, _ = offline
    source = public_source(kind, monkeypatch, download_pdfs=False)
    result = SearchResult(title="Portfolio risk", abstract_only=True,
        source=SourceType.BIGQUANT if kind == "bigquant" else SourceType.EASTMONEY,
        source_url="https://bigquant.com/wiki/doc/fixture" if kind == "bigquant"
            else "https://data.eastmoney.com/report/info/A1.html")
    hydrated = asyncio.run(source.hydrate_result(result))
    assert any(is_pdf(request) for request in requests)
    assert not hydrated.abstract_only and Path(hydrated.pdf_path).is_file()
    assert "Complete PDF evidence" in hydrated.full_text


def test_arxiv_explicit_hydration_fetches_only_original_revision_pdf(offline):
    requests, _, directory = offline
    source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    result = asyncio.run(source.search(UserCriteria(topics=["portfolio"])))[0]
    assert not any(is_pdf(request) for request in requests)
    assert not list(directory.rglob("*.pdf"))
    requests.clear()
    hydrated = asyncio.run(source.hydrate_result(result))
    assert [str(request.url) for request in requests] == ["https://arxiv.org/pdf/2610.00001v2"]
    assert hydrated.arxiv_id == "2610.00001v2"
    assert hydrated.source_url == result.source_url
    assert not hydrated.abstract_only and Path(hydrated.pdf_path).is_file()
    assert "Complete PDF evidence" in hydrated.full_text


@pytest.mark.parametrize("kind,discover_links", [
    ("arxiv", False), ("eastmoney", False), ("bigquant", False),
    ("eastmoney", True), ("bigquant", True),
])
def test_explicit_hydration_can_isolate_pdf_in_a_temporary_directory(
    offline, monkeypatch, kind, discover_links,
):
    _, _, directory = offline
    if kind == "arxiv":
        source = ArxivSource(rate_limit_seconds=0, download_pdfs=False)
    else:
        source = public_source(kind, monkeypatch, download_pdfs=False)
    result = asyncio.run(source.search(UserCriteria(topics=["portfolio"])))[0]
    if discover_links:
        # A single-report read may start before attachment links are discovered.
        result = result.model_copy(update={"raw_metadata": {}})
    staging = directory / "screening" / "candidate"
    hydrated = asyncio.run(source.hydrate_result(result, pdf_directory=staging))
    assert not hydrated.abstract_only and "Complete PDF evidence" in hydrated.full_text
    assert Path(hydrated.pdf_path).parent == staging
    assert Path(hydrated.pdf_path).is_file()
    assert not (directory / "pdfs").exists()
    assert not (directory / "data" / "pdfs").exists()
    assert list(directory.rglob("*.pdf")) == [Path(hydrated.pdf_path)]
