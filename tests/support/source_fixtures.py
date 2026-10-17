"""Offline collection fixtures and source response builders shared by regressions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from reportagent.models.schemas import SearchResult, SourceType
from reportagent.services import collection
from tests.support.collection_scoring import RelevanceModel


@pytest.fixture
def collect(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'reports.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(collection, "init_db", lambda: None)
    monkeypatch.setattr(collection, "get_session_factory", lambda: factory)

    monkeypatch.setattr(collection, "LLMClient", RelevanceModel)
    # These cases exercise source health with the original rule classification.
    monkeypatch.setattr(collection, "LLMClassifier", lambda client: None)

    def run(sources, **kwargs):
        monkeypatch.setattr(collection, "build_sources", lambda names: sources)
        names = ",".join(s.source_type.value for s in sources)
        result = asyncio.run(
            collection.collect_reports(
                collection.CollectionOptions(
                    topics="portfolio",
                    sources=names,
                    no_analysis=True,
                    days=0,
                    **kwargs,
                )
            )
        )
        with factory() as session:
            rows = session.query(Report).all()
        return result, rows

    yield run
    engine.dispose()


def paper(source=SourceType.ARXIV, **kwargs):
    values = dict(
        title="Portfolio optimization and quantitative risk",
        abstract="A finance portfolio risk model",
        source=source,
        source_url="https://example.org/paper/1",
        abstract_only=True,
    )
    values.update(kwargs)
    return SearchResult(**values)


def fake(source, rows=None, error=None, available=True):
    return SimpleNamespace(
        source_type=source,
        is_available=lambda: available,
        search=AsyncMock(side_effect=error, return_value=rows or []),
    )


def mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


def atom_entry(identifier, published="2026-10-16T00:00:00Z"):
    return f"""<entry><id>http://arxiv.org/abs/{identifier}</id>
    <title>Portfolio optimization quantitative risk model</title>
    <updated>2026-10-16T00:00:00Z</updated><published>{published}</published>
    <summary>A finance portfolio risk model with empirical backtests.</summary>
    <author><name>Researcher</name></author>
    <link title="pdf" href="https://arxiv.org/pdf/{identifier}" rel="related" type="application/pdf"/>
    <category term="q-fin.PM"/></entry>"""


def atom_feed(entries, total):
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<opensearch:totalResults>{total}</opensearch:totalResults>{entries}</feed>"
    )


def bigquant_info():
    return httpx.Response(200, json={"ok": True, "data": {"id": "public-collection"}})


def bigquant_document(
    name,
    *,
    title="portfolio risk model",
    text="Portfolio covariance and risk evidence. " * 5,
    published="2026-10-15T01:00:00Z",
    original=None,
):
    if original:
        text = f"原报告发布日期：{original}\n" + text
    return {
        "id": name,
        "urlId": name,
        "title": title,
        "text": text,
        "publishedAt": published,
    }


def bigquant_page(rows, next_offset=None):
    return httpx.Response(
        200,
        json={
            "ok": True,
            "data": rows,
            "pagination": {
                "nextPath": None
                if next_offset is None
                else f"/api/documents.list?offset={next_offset}"
            },
        },
    )
