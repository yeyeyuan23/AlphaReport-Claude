"""Dates must survive source parsing and re-import without turning reposts into new work."""

from reportagent.sources.date_window import source_date


import asyncio
from datetime import datetime
from unittest.mock import patch

import httpx
import pytest

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Report
from reportagent.models.schemas import (
    ClassifiedReport,
    ClassificationResult,
    SearchResult,
    SourceType,
)
from reportagent.services import weekly_report
from reportagent.sources.bigquant_source import BigQuantSource


def fetch(body):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        ) as client:
            return await BigQuantSource()._fetch_detail(
                client, "Fixture paper", "/wiki/doc/fixture"
            )

    return asyncio.run(run())


def page(content="", head="", time=""):
    return (
        f"<html><head>{head}</head><article><h1>Portfolio risk model</h1>"
        f"<div>由author创建，最终由editor{time}</div>"
        f"<p>A quantitative portfolio model with enough abstract content.</p>{content}"
        "</article></html>"
    )


def report(date=None, arxiv_id=None):
    return ClassifiedReport(
        search_result=SearchResult(
            title="Portfolio risk model",
            authors=["author"],
            abstract="Same abstract",
            source=SourceType.ARXIV if arxiv_id else SourceType.BIGQUANT,
            source_url="https://bigquant.com/wiki/doc/fixture",
            published_date=date,
            arxiv_id=arxiv_id,
            abstract_only=True,
        ),
        classification=ClassificationResult(),
    )


@pytest.fixture
def library(memory_session_factory):
    yield memory_session_factory


def test_source_publication_date_wins_over_cited_original_and_update_dates():
    result = fetch(
        page(
            "<p>本文来自方正证券研究所于2022年8月4日发布的报告《多因子研究》。</p>",
            head='<meta property="article:published_time" content="2024-06-18T06:09:00Z">',
            time='<time datetime="1767060353">更新于2025-12-30 02:05</time>',
        )
    )
    assert result.published_date == source_date(datetime(2024, 6, 18))
    assert result.raw_metadata["published_date_source"] == "meta:article:published_time"


@pytest.mark.parametrize(
    "head, expected",
    [
        (
            '<meta property="article:published_time" content="2023-02-15T09:30:00+08:00">',
            datetime(2023, 2, 15, 1, 30),
        ),
        (
            '<meta name="citation_publication_date" content="2023/2/15">',
            None,
        ),
        (
            '<script type="application/ld+json">{"@type":"Article","datePublished":"2023-02-15","dateModified":"2026-10-16"}</script>',
            datetime(2023, 2, 15),
        ),
    ],
)
def test_explicit_publication_metadata_is_supported(head, expected):
    assert fetch(page(head=head)).published_date == source_date(expected)


def test_explicit_published_time_tag_is_supported():
    result = fetch(
        page(
            time='<time itemprop="datePublished" datetime="2023-02-15">发表于2023-02-15</time>'
        )
    )
    assert result.published_date == source_date(datetime(2023, 2, 15))


@pytest.mark.parametrize(
    "content, head, time",
    [
        (
            "",
            '<meta property="article:modified_time" content="2026-10-16">',
            '<time datetime="1767060353">更新于2025-12-30</time>',
        ),
        (
            "<p>样本区间为2013年1月1日至2022年7月31日。参考文献发表时间为2021年。</p>",
            "",
            "",
        ),
        ("<p>本文来自研究所于2023年2月30日发布的报告。</p>", "", ""),
        (
            "",
            '<script type="application/ld+json">{broken JSON}</script>',
            '<time datetime="2024-06-18">更新于2024-06-18</time>',
        ),
    ],
)
def test_update_sample_and_invalid_dates_never_become_publication_dates(
    content, head, time
):
    assert fetch(page(content=content, head=head, time=time)).published_date is None


def test_reimport_backfills_date_without_clearing_analysis_or_relevance(library):
    with library() as session:
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(report())
        row = session.get(Report, rid)
        row.analysis_json = "saved analysis"
        row.evaluation_json = "saved evaluation"
        row.knowledge_card_json = "saved card"
        row.selection_relevance = 8
        session.commit()
        action, same_id = repo.upsert_report(report(datetime(2022, 8, 4)))
        session.expire_all()
        row = session.get(Report, rid)
        assert action == "updated" and same_id == rid
        assert row.published_date == source_date(datetime(2022, 8, 4))
        assert (row.analysis_json, row.evaluation_json, row.knowledge_card_json) == (
            "saved analysis",
            "saved evaluation",
            "saved card",
        )
        assert row.selection_relevance == 8
        assert session.query(Report).count() == 1


@pytest.mark.parametrize("new_date", [None, datetime(2024, 2, 3)])
def test_reimport_local_pdf_repairs_legacy_creation_date_and_fake_abstract(
    library, new_date
):
    def local(date, abstract, checked=False):
        return ClassifiedReport(
            search_result=SearchResult(
                title="Portfolio risk model",
                authors=["author"],
                abstract=abstract,
                source=SourceType.LOCAL_PDF,
                pdf_path="/library/paper.pdf",
                full_text="A full financial risk paper with sufficient research evidence. "
                * 5,
                published_date=date,
                raw_metadata=(
                    {"publication_date_checked": True, "abstract_source": "unavailable"}
                    if checked
                    else {}
                ),
            ),
            classification=ClassificationResult(),
        )

    with library() as session:
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(
            local(datetime(2026, 1, 2), "Title authors and arbitrary leading text")
        )
        # Provenance identifies this as a legacy file-creation timestamp.
        session.get(
            Report, rid
        ).publication_date_info_json = '{"source": "pdf_creation_date"}'
        session.commit()
        repo.upsert_report(local(new_date, None, checked=True))
        row = session.get(Report, rid)
        assert row.published_date == source_date(new_date)
        assert row.abstract is None


def test_unknown_or_repost_date_does_not_replace_known_same_revision_date(library):
    with library() as session:
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(report(datetime(2022, 8, 4)))
        repo.upsert_report(report(None))
        repo.upsert_report(report(datetime(2025, 12, 30)))
        assert session.get(Report, rid).published_date == source_date(
            datetime(2022, 8, 4)
        )


def test_new_and_delayed_arxiv_revisions_cannot_replace_known_publication_date(library):
    with library() as session:
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(report(datetime(2025, 1, 1), "2501.00001v1"))
        repo.upsert_report(report(datetime(2026, 10, 9), "2501.00001v2"))
        assert session.get(Report, rid).published_date == source_date(
            datetime(2025, 1, 1)
        )
        assert session.get(Report, rid).arxiv_id == "2501.00001v2"
        repo.upsert_report(report(datetime(2025, 1, 1), "2501.00001v1"))
        assert session.get(Report, rid).published_date == source_date(
            datetime(2025, 1, 1)
        )


def test_source_page_date_persists_and_drives_report_instead_of_cited_report(library):
    fetched = fetch(page(
        "<p>原报告发布日期：2022年8月4日</p>",
        head='<meta property="article:published_time" content="2026-10-09">',
    ))
    with library() as session:
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(ClassifiedReport(search_result=fetched, classification=ClassificationResult()))
        assert session.get(Report, rid).published_date == source_date("2026-10-09")
    with (patch.object(weekly_report, "init_db"), patch.object(weekly_report, "get_session_factory", return_value=library)):
        papers = weekly_report.get_weekly_papers(datetime(2026, 10, 5), datetime(2026, 10, 12))
        assert [p["id"] for p in papers] == [rid]


def test_invalid_optional_jsonld_type_does_not_drop_the_article():
    result = fetch(
        page(
            head='<script type="application/ld+json">'
            '{"@type":[{}],"datePublished":"2023-02-15"}</script>'
        )
    )
    assert result.title == "Portfolio risk model"
    assert result.published_date is None


def test_invalid_original_report_date_does_not_fall_back_to_repost_date():
    result = fetch(
        page(
            "<p>本文来自研究所于2023年2月30日发布的报告。</p>",
            head='<meta property="article:published_time" content="2026-10-16">',
        )
    )
    assert result.published_date == source_date("2026-10-16")


def test_jsonld_uses_current_article_instead_of_cited_paper():
    result = fetch(
        page(
            head='<script type="application/ld+json">{"@graph":['
            '{"@type":"ScholarlyArticle","@id":"https://cited.example/paper","datePublished":"2019-01-02"},'
            '{"@type":"Article","@id":"https://bigquant.com/wiki/doc/fixture#article","datePublished":"2024-03-04"}'
            "]}</script>"
        )
    )
    assert result.published_date == source_date(datetime(2024, 3, 4))


def test_comment_publication_time_is_not_the_article_publication_time():
    result = fetch(
        page(
            content='<div itemscope itemtype="https://schema.org/Comment">'
            '<time itemprop="datePublished" datetime="2026-10-16">评论发表于2026-10-16</time></div>'
        )
    )
    assert result.published_date is None


def test_reference_publication_date_is_not_original_report_date():
    result = fetch(
        page(
            content="<p>参考文献：论文发布日期：2021年1月2日。</p>",
            head='<meta property="article:published_time" content="2024-03-04">',
        )
    )
    assert result.published_date == source_date(datetime(2024, 3, 4))


def test_ambiguous_anonymous_article_dates_are_not_guessed():
    result = fetch(
        page(
            head='<script type="application/ld+json">['
            '{"@type":"Article","datePublished":"2019-01-02"},'
            '{"@type":"Article","datePublished":"2024-03-04"}]</script>'
        )
    )
    assert result.published_date is None
