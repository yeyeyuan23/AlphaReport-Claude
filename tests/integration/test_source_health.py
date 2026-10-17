"""Offline source observations must reflect real requests and preserve useful rows."""

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from reportagent.models.schemas import SourceType
from reportagent.sources.arxiv_source import ArxivSource
from reportagent.sources.bigquant_source import BigQuantSource
from reportagent.sources.eastmoney_source import EastMoneySource
from reportagent.sources.local_pdf import LocalPDFSource
from tests.support.source_fixtures import (
    collect as collection_fixture,
    fake,
    paper,
    mock_http,
    atom_entry,
    atom_feed,
)


# Pytest discovers imported fixtures through module attributes.
collect = collection_fixture


def test_mixed_sources_keep_errors_after_storage_and_redact_secrets(collect):
    secret = "credential-that-must-never-appear"
    result, rows = collect(
        [
            fake(SourceType.ARXIV, [paper()]),
            fake(
                SourceType.EASTMONEY,
                error=RuntimeError(
                    f"request failed https://example.org?querytoken={secret} Authorization: Bearer {secret}"
                ),
            ),
            fake(SourceType.LOCAL_PDF, available=False),
        ]
    )
    assert result.get("status") == "partial"
    assert [s["status"] for s in result["source_statuses"]] == [
        "ok",
        "error",
        "unavailable",
    ]
    assert result["storage"]["added"] == 1 and len(rows) == 1
    assert result["storage"]["errors"]
    assert secret not in json.dumps(result)
    assert all(s["elapsed_ms"] >= 0 for s in result["source_statuses"])


def test_true_empty_is_success_but_all_failed_is_failed(collect):
    result, rows = collect([fake(SourceType.ARXIV)])
    assert result.get("status") == "ok"
    assert result["source_statuses"][0]["status"] == "empty"
    assert rows == []
    result, _ = collect([fake(SourceType.ARXIV, error=TimeoutError("network timeout"))])
    assert result.get("status") == "failed"
    assert result["source_statuses"][0]["status"] == "error"


def test_canonical_id_dedup_preserves_same_titles_with_different_ids(collect):
    result, rows = collect(
        [
            fake(
                SourceType.ARXIV,
                [
                    paper(
                        arxiv_id="2610.00001v1",
                        source_url="http://arxiv.org/abs/2610.00001v1",
                    ),
                    paper(
                        arxiv_id="2610.00001v1",
                        source_url="https://arxiv.org/abs/2610.00001v1#section",
                    ),
                    paper(
                        arxiv_id="2610.00002v1",
                        source_url="https://arxiv.org/abs/2610.00002v1",
                        full_text="Full finance paper text and financial methodology. "
                        * 4,
                        abstract_only=False,
                    ),
                ],
            )
        ]
    )
    status = result.get("source_statuses", [{}])[0]
    assert (status.get("fetched"), status.get("valid"), status.get("full_text")) == (
        3,
        2,
        1,
    )
    assert result["results_count"] == 2


def test_eastmoney_retains_first_page_when_next_page_fails(collect, monkeypatch):
    def respond(request):
        if "/report/info/" in request.url.path:
            return httpx.Response(
                200,
                text="<article><p>We estimate portfolio covariance from market returns and compare financial risk budgets.</p></article>",
            )
        if request.url.params["pageNo"] == "1":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "title": "组合优化 portfolio optimization quantitative risk model",
                            "infoCode": "A1",
                        },
                    ],
                    "TotalPage": 2,
                },
            )
        return httpx.Response(503, text="unavailable")

    mock_http(monkeypatch, respond)
    source = EastMoneySource(rate_limit_seconds=0)
    monkeypatch.setattr(source, "_resolve_qtypes", lambda criteria: [2])
    result, rows = collect([source])
    assert result["storage"]["added"] == 1 and len(rows) == 1
    status = result["source_statuses"][0]
    assert status["status"] == "partial" and status["fetched"] == 1
    assert any("503" in error for error in status["errors"])


@pytest.mark.parametrize(
    "payload",
    [{"error": "rate limited"}, {"data": None}, {"data": "not a list"}, [1, 2]],
)
def test_eastmoney_bad_json_shape_is_not_empty(collect, monkeypatch, payload):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    result, _ = collect([EastMoneySource(rate_limit_seconds=0)])
    assert result.get("status") == "failed"
    assert result["source_statuses"][0]["status"] == "error"


def test_eastmoney_true_empty_and_diagnostics_reset(collect, monkeypatch):
    payloads = iter(
        [
            httpx.Response(200, text="<html>captcha</html>"),
            httpx.Response(200, json={"data": [], "TotalPage": 0}),
        ]
    )
    mock_http(monkeypatch, lambda request: next(payloads))
    source = EastMoneySource(rate_limit_seconds=0)
    result, _ = collect([source])
    assert result.get("status") == "failed"
    result, _ = collect([source])
    assert result["source_statuses"][0]["status"] == "empty"
    assert result["source_statuses"][0]["errors"] == []


def test_eastmoney_bad_item_does_not_discard_siblings(collect, monkeypatch):
    def respond(request):
        if "/report/info/" in request.url.path:
            return httpx.Response(
                200,
                text="<article><p>We estimate portfolio covariance from market returns and compare financial risk budgets.</p></article>",
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "title": "组合优化 portfolio optimization quantitative risk model",
                        "infoCode": "A1",
                    },
                    {"title": ""},
                    "malformed item",
                ],
                "TotalPage": 1,
            },
        )

    mock_http(monkeypatch, respond)
    result, rows = collect([EastMoneySource(rate_limit_seconds=0)])
    assert len(rows) == 1
    status = result["source_statuses"][0]
    assert status["status"] == "partial"
    assert (status["fetched"], status["valid"]) == (3, 1)


@pytest.mark.parametrize(
    "body", ["<html><body>Login required</body></html>", '{"blocked":true}']
)
def test_bigquant_unrecognized_listing_is_parse_failure(collect, monkeypatch, body):
    mock_http(monkeypatch, lambda request: httpx.Response(200, text=body))
    result, _ = collect([BigQuantSource(rate_limit_seconds=0, max_listing_pages=1)])
    assert result.get("status") == "failed"
    assert result["source_statuses"][0]["status"] == "error"


def test_bigquant_dedup_and_bad_detail_preserve_good_article(collect, monkeypatch):
    def respond(request):
        if "/collection/" in request.url.path:
            return httpx.Response(
                200,
                text='<html><body><a href="/wiki/doc/good">portfolio risk</a>'
                '<a href="/wiki/doc/good">portfolio risk duplicate</a>'
                '<a href="/wiki/doc/bad">portfolio risk bad</a></body></html>',
            )
        if request.url.path.endswith("good"):
            return httpx.Response(
                200,
                text="<html><h1>Portfolio optimization</h1><article><p>"
                "A quantitative finance portfolio risk model and its methodology."
                "</p></article></html>",
            )
        return httpx.Response(200, text="<html><h1>Login required</h1></html>")

    mock_http(monkeypatch, respond)
    result, rows = collect([BigQuantSource(rate_limit_seconds=0, max_listing_pages=1)])
    assert len(rows) == 1
    status = result.get("source_statuses", [{}])[0]
    assert status.get("status") == "partial"
    assert (status["fetched"], status["valid"]) == (3, 1)


def mock_arxiv_feed(monkeypatch, body):
    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            ),
            **kwargs,
        ),
    )


@pytest.mark.parametrize(
    "body",
    [
        "<html><body>rate limit</body></html>",
        "<feed",
        "<feed xmlns='http://www.w3.org/2005/Atom'></feed>",
    ],
)
def test_arxiv_malformed_or_non_atom_response_is_not_empty(collect, monkeypatch, body):
    mock_arxiv_feed(monkeypatch, body)
    result, _ = collect([ArxivSource(rate_limit_seconds=0, download_pdfs=False)])
    assert result.get("status") == "failed"
    assert result["source_statuses"][0]["status"] == "error"


def test_arxiv_valid_empty_feed_is_empty(collect, monkeypatch):
    mock_arxiv_feed(
        monkeypatch,
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        "<opensearch:totalResults>0</opensearch:totalResults></feed>",
    )
    result, _ = collect([ArxivSource(rate_limit_seconds=0, download_pdfs=False)])
    assert result.get("status") == "ok"
    assert result["source_statuses"][0]["status"] == "empty"


def test_non_pdf_download_preserves_abstract_and_reports_partial(collect, monkeypatch):
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
        monkeypatch, lambda request: httpx.Response(200, text="<html>captcha</html>")
    )
    with patch.object(
        source,
        "_save_pdf_bytes",
        side_effect=AssertionError("Invalid bytes must not be saved"),
    ):
        result, rows = collect([source])
    assert len(rows) == 1 and not rows[0].full_text
    status = result.get("source_statuses", [{}])[0]
    assert status.get("status") == "partial" and status["full_text"] == 0
    assert any("pdf" in error.lower() for error in status["errors"])


def test_local_bad_pdf_is_error_not_empty(collect, tmp_path):
    (tmp_path / "bad.pdf").write_text("<html>not a PDF</html>")
    result, rows = collect([LocalPDFSource(str(tmp_path))])
    assert result.get("status") == "failed"
    assert result["source_statuses"][0]["fetched"] == 1
    assert rows == []


def test_arxiv_bad_entry_date_does_not_erase_good_sibling(collect, monkeypatch):
    mock_arxiv_feed(
        monkeypatch,
        atom_feed(
            atom_entry("2610.00001v1")
            + atom_entry("2610.00002v1", published="malformed-date"),
            2,
        ),
    )
    result, rows = collect([ArxivSource(rate_limit_seconds=0, download_pdfs=False)])
    assert len(rows) == 1
    status = result["source_statuses"][0]
    assert (status["status"], status["fetched"], status["valid"]) == ("partial", 2, 1)


def test_arxiv_later_feed_failure_preserves_prior_page(collect, monkeypatch):
    bodies = iter([atom_feed(atom_entry("2610.00001v1"), 2), "<html>gateway</html>"])

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=next(bodies))
            ),
            **kwargs,
        ),
    )
    result, rows = collect([ArxivSource(rate_limit_seconds=0, download_pdfs=False)])
    assert len(rows) == 1
    assert result["source_statuses"][0]["status"] == "partial"


def test_unusable_pdf_extraction_does_not_count_as_full_text(collect, monkeypatch):
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
        monkeypatch,
        lambda request: httpx.Response(200, content=b"%PDF-1.4 test fixture"),
    )
    monkeypatch.setattr(source, "_save_pdf_bytes", lambda *args: "/fixture/paper.pdf")
    monkeypatch.setattr(
        source,
        "_parse_pdf_bytes",
        lambda data: "<html><body>blocked</body></html>" * 10,
    )
    result, rows = collect([source])
    assert len(rows) == 1 and rows[0].full_text is None
    status = result["source_statuses"][0]
    assert (status["status"], status["full_text"]) == ("partial", 0)


def test_bigquant_explicit_empty_state_is_empty(collect, monkeypatch):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, text="<html><h1>Wiki</h1>暂无文档</html>"),
    )
    result, _ = collect([BigQuantSource(rate_limit_seconds=0, max_listing_pages=1)])
    assert result["source_statuses"][0]["status"] == "empty"


def test_valid_count_excludes_relevance_rejections(collect):
    result, rows = collect(
        [
            fake(
                SourceType.ARXIV,
                [
                    paper(arxiv_id="2610.00001v1"),
                    paper(
                        title="Medieval poetry",
                        abstract="Historical texts",
                        arxiv_id="2610.00002v1",
                    ),
                ],
            )
        ]
    )
    assert len(rows) == 1
    status = result["source_statuses"][0]
    assert (status["fetched"], status["valid"]) == (2, 1)


def test_arxiv_overall_timeout_preserves_pages_already_parsed(collect, monkeypatch):
    import time

    def respond(request):
        if int(request.url.params["start"]) == 0:
            return httpx.Response(200, text=atom_feed(atom_entry("2610.00001v1"), 2))
        time.sleep(0.05)
        return httpx.Response(200, text=atom_feed(atom_entry("2610.00002v1"), 2))

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    wait_for = asyncio.wait_for

    async def bounded_wait(awaitable, timeout):
        return await wait_for(awaitable, timeout=0.02 if timeout == 120 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", bounded_wait)
    result, rows = collect([ArxivSource(rate_limit_seconds=0, download_pdfs=False)])
    assert len(rows) == 1 and rows[0].arxiv_id == "2610.00001v1"
    assert result["source_statuses"][0]["status"] == "partial"
    assert any("Timeout" in error for error in result["source_statuses"][0]["errors"])
