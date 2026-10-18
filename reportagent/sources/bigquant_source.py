from __future__ import annotations

from datetime import date, datetime
import json
import logging
from pathlib import Path
import re
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from reportagent.models.schemas import UserCriteria, SearchResult, SourceType
from reportagent.sources.base import BaseSource, SourceParseError, canonical_url
from reportagent.utils.topic_expansion import expand_topics
from reportagent.sources.date_window import (
    candidate_in_window,
    in_date_window,
)
from reportagent.processors.paper_sections import research_excerpt
from reportagent.sources.bigquant_catalog import discover_catalog
from reportagent.sources.base import usable_full_text
from reportagent.sources.content_fetch import (
    article_text,
    bounded_download,
    hydrate_pdf,
    meaningful_excerpt,
    pdf_links,
)

from reportagent.utils.runtime_limits import (
    metadata_max_bytes,
    metadata_timeout_seconds,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://bigquant.com"
DEFAULT_RATE_LIMIT = 2.0

AUTHOR_PATTERN = re.compile(r"由(.+?)创建")
DATE_TEXT = r"\d{4}\s*(?:年|[-/])\s*\d{1,2}\s*(?:月|[-/])\s*\d{1,2}\s*日?"


def _parse_publication_date(value) -> Optional[date]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if re.fullmatch(DATE_TEXT, value):
            stamp = datetime(*(int(n) for n in re.findall(r"\d+", value)))
        else:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.date()
    except ValueError:
        return None


def _publication_date(
    soup: BeautifulSoup,
    article,
    page_url: str = "",
    *,
    own_page_only: bool = False,
    evidence: dict | None = None,
) -> tuple[Optional[date], Optional[str]]:
    """Read original-report evidence, or explicitly the source page's own date."""

    def parse(value):
        return _parse_publication_date(value)

    text = article.get_text(" ", strip=True) if article and not own_page_only else ""
    text = re.split(r"参考文献|引用文献|\bReferences\b", text, maxsplit=1, flags=re.I)[
        0
    ]
    patterns = (
        rf"(?:本文(?:来自|转载自)|原(?:始)?(?:报告|研报|论文)|本(?:报告|研报|论文))"
        rf"[^。；\n]{{0,180}}?(?:于|[:：])\s*(?P<date>{DATE_TEXT})\s*(?:发布|发表|出具|刊发)",
        rf"(?:原(?:始)?|本)(?:报告|研报|论文)(?:发表|发布|出版|出具)?日期"
        rf"\s*[:：]?\s*(?P<date>{DATE_TEXT})",
    )
    original_date_found = False
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            original_date_found = True
            stamp = parse(match["date"])
            if stamp is not None:
                return stamp, "original_report_text"
    if original_date_found:
        # Invalid original-report evidence cannot be repaired by silently using
        # the repost page's publication date instead.
        return None, None

    for name in (
        "citation_publication_date",
        "citation_date",
        "article:published_time",
        "datePublished",
    ):
        if own_page_only and name.startswith("citation_"):
            continue
        for tag in soup.find_all("meta"):
            if (tag.get("name") or tag.get("property") or "").lower() == name.lower():
                stamp = parse(tag.get("content"))
                if stamp is not None:
                    if evidence is not None:
                        evidence["evidence"] = tag.get("content")
                    return stamp, f"meta:{name}"

    # Only read an Article/WebPage object's own datePublished, not dates from its
    # comments, cited references, or dateModified fields.
    def objects(value):
        if isinstance(value, list):
            for item in value:
                yield from objects(item)
        elif isinstance(value, dict):
            kind = value.get("@type", [])
            kinds = (
                {kind}
                if isinstance(kind, str)
                else {k for k in kind if isinstance(k, str)}
                if isinstance(kind, list)
                else set()
            )
            if kinds & {
                "Article",
                "BlogPosting",
                "NewsArticle",
                "ScholarlyArticle",
                "WebPage",
            }:
                yield value
            yield from objects(value.get("@graph", []))

    candidates = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for item in objects(data):
            identifiers = [
                item[key] for key in ("url", "@id") if isinstance(item.get(key), str)
            ]
            if (
                page_url
                and identifiers
                and not any(
                    canonical_url(urljoin(page_url, value)) == canonical_url(page_url)
                    for value in identifiers
                )
            ):
                continue
            stamp = parse(item.get("datePublished"))
            if stamp is not None:
                candidates.append(
                    (bool(identifiers and page_url), stamp, item.get("datePublished"))
                )
    if candidates:
        matching_page = [stamp for identified, stamp, _ in candidates if identified]
        dates = set(matching_page or [stamp for _, stamp, _ in candidates])
        if len(dates) == 1:
            stamp = dates.pop()
            if evidence is not None:
                evidence["evidence"] = next(
                    raw for _, candidate, raw in candidates if candidate == stamp
                )
            return stamp, "jsonld:datePublished"

    for tag in article.find_all("time") if article else []:
        scope = tag.find_parent(attrs={"itemscope": True})
        if (
            own_page_only
            and scope
            and page_url
            and isinstance(scope.get("itemid"), str)
            and canonical_url(urljoin(page_url, scope["itemid"]))
            != canonical_url(page_url)
        ):
            continue
        if (
            scope
            and scope.get("itemtype")
            and not re.search(
                r"(?:^|[/#])(?:Article|BlogPosting|NewsArticle|ScholarlyArticle|WebPage)$",
                scope["itemtype"],
            )
        ):
            continue
        if "datePublished" in (tag.get("itemprop") or "").split():
            stamp = parse(tag.get("datetime") or tag.get_text(strip=True))
            if stamp is not None:
                if evidence is not None:
                    evidence["evidence"] = tag.get("datetime") or tag.get_text(
                        strip=True
                    )
                return stamp, "time:datePublished"
    return None, None


class BigQuantSource(BaseSource):
    def __init__(
        self,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT,
        download_pdfs: bool = True,
    ):
        self.rate_limit_seconds = rate_limit_seconds
        self.download_pdfs = download_pdfs

    @property
    def source_type(self) -> SourceType:
        return SourceType.BIGQUANT

    def is_available(self) -> bool:
        return True

    async def hydrate_result(
        self,
        result: SearchResult,
        *,
        pdf_directory: Path | None = None,
    ) -> SearchResult:
        """Re-fetch one public article's evidence without models or database access."""
        async with httpx.AsyncClient(
            timeout=metadata_timeout_seconds(), follow_redirects=True
        ) as client:
            links = result.raw_metadata.get("pdf_links") or []
            if not links and result.raw_metadata.get("pdf_link"):
                links = [result.raw_metadata["pdf_link"]]
            if links:
                return await hydrate_pdf(
                    client, result, links, self.last_search, pdf_directory=pdf_directory
                )
            return await self._fetch_detail(
                client,
                result.title,
                result.source_url or result.raw_metadata["doc_path"],
                download_pdfs=True,
                pdf_directory=pdf_directory,
            )

    async def search(self, criteria: UserCriteria) -> list[SearchResult]:
        """Normalize source metadata; shared policy decides date eligibility."""
        self._start_search()
        terms = [
            t.lower()
            for t in expand_topics(criteria.topics, criteria.keywords, lang="all")
        ]
        results = []
        async with httpx.AsyncClient(
            timeout=metadata_timeout_seconds(), follow_redirects=True
        ) as client:
            try:
                async for row, stamp in discover_catalog(
                    client, criteria, self.last_search, self.rate_limit_seconds
                ):
                    result = await self._prepare_candidate(
                        client, row, stamp, criteria, terms
                    )
                    if result is not None:
                        results.append(result)
                    if (
                        criteria.max_results_per_source is not None
                        and len(results) >= criteria.max_results_per_source
                    ):
                        break
            except Exception as error:
                self.last_search.record_error(error, "catalog search")
        return results

    async def _prepare_candidate(self, client, row, stamp, criteria, terms):
        if (
            criteria.date_basis != "announcement"
            and terms
            and not self._score(str(row.get("title", "")).lower(), terms)
        ):
            return None
        try:
            result = self._catalog_result(row, stamp)
            if not candidate_in_window(result, criteria):
                return None
            if self.download_pdfs and result.raw_metadata["pdf_links"]:
                result = await hydrate_pdf(
                    client, result, result.raw_metadata["pdf_links"], self.last_search
                )
            return result
        except Exception as error:
            self.last_search.record_error(error, "document metadata")
            return None

    def _catalog_result(self, row: dict, stamp: datetime) -> SearchResult:
        title, slug, raw = row.get("title"), row.get("urlId"), row.get("text")
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(slug, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", slug)
        ):
            raise SourceParseError("catalog missing document title or public identity")
        raw = raw if isinstance(raw, str) else ""
        url = f"{BASE_URL}/wiki/doc/{slug}"
        soup = BeautifulSoup(raw, "html.parser")
        links = self._catalog_links(raw, soup, url)
        text = self._catalog_text(raw)
        abstract = research_excerpt(text)
        if not abstract and not links:
            raise SourceParseError(
                "catalog document has no research text or public PDF"
            )
        return SearchResult(
            title=title.strip(),
            source=SourceType.BIGQUANT,
            source_url=url,
            abstract=abstract,
            full_text=text if usable_full_text(text) else None,
            abstract_only=bool(links) or not usable_full_text(text),
            published_date=stamp,
            raw_metadata={
                "doc_path": f"/wiki/doc/{slug}",
                "source_item_id": row["id"],
                "pdf_link": links[0] if links else None,
                "pdf_links": links,
                "publication_date_info": {
                    "precision": "day",
                    "source": "bigquant_api:publishedAt",
                    "evidence": row["publishedAt"],
                },
                "content_kind": "html" if text else "metadata_only",
                "text_scope": "html_article" if text else "metadata_only",
            },
        )

    @staticmethod
    def _catalog_links(raw, soup, url):
        links = pdf_links(soup, url)
        for match in re.finditer(r"\[[^\]]*\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)", raw):
            link = urljoin(url, match[1])
            if (
                urlsplit(link).scheme in {"http", "https"}
                and ".pdf" in urlsplit(link).path.lower()
                and link not in links
            ):
                links.append(link)
        return links

    @staticmethod
    def _catalog_text(raw):
        body = re.sub(
            r"\[[^\]]*\]\(([^\s)]*\.pdf(?:\?[^\s)]*)?)(?:\s+\"[^\"]*\")?\)",
            "",
            raw,
            flags=re.I,
        )
        return article_text(BeautifulSoup(body, "html.parser"))

    async def _fetch_detail(
        self,
        client: httpx.AsyncClient,
        title: str,
        doc_path: str,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        date_basis: str = "publication",
        download_pdfs: bool | None = None,
        pdf_directory: Path | None = None,
    ) -> Optional[SearchResult]:
        url = f"{BASE_URL}{doc_path}" if doc_path.startswith("/") else doc_path
        body = await bounded_download(client, url, metadata_max_bytes())
        soup = BeautifulSoup(body, "html.parser")

        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

        abstract = None
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            abstract = meaningful_excerpt(meta_desc["content"])
            if abstract and "BigQuant" in abstract and "知识库" in abstract:
                abstract = None

        authors: list[str] = []
        article = soup.find("article")
        if article:
            raw_article_text = article.get_text()
            m = AUTHOR_PATTERN.search(raw_article_text)
            if m:
                authors = [a.strip() for a in m.group(1).split(",") if a.strip()]

        if not h1 or not title.strip() or not article:
            raise SourceParseError("unrecognized HTML article or missing title")

        date_info = {"evidence": None}
        published_date, date_source = _publication_date(
            soup, article, url, own_page_only=True, evidence=date_info
        )
        date_info.update(
            source=date_source, precision="day" if published_date else "unknown"
        )
        if published_date is None and (date_from or date_to):
            self.last_search.record_error(
                SourceParseError("publication date unavailable"), "article date"
            )
            return None
        if not in_date_window(published_date, date_from, date_to):
            return None
        text = article_text(article)
        abstract = research_excerpt(text) or abstract
        links = pdf_links(article, url)

        result = SearchResult(
            title=title,
            authors=authors,
            abstract=abstract,
            full_text=text if usable_full_text(text) else None,
            abstract_only=bool(links) or not usable_full_text(text),
            source=SourceType.BIGQUANT,
            source_url=url,
            published_date=published_date,
            raw_metadata={
                "doc_path": doc_path,
                "pdf_link": links[0] if links else None,
                "pdf_links": links,
                "published_date_source": date_source,
                "publication_date_info": date_info,
                "content_kind": "html" if text else "metadata_only",
                "text_scope": "html_article" if text else "metadata_only",
            },
        )
        should_download = self.download_pdfs if download_pdfs is None else download_pdfs
        if links and should_download:
            result = await hydrate_pdf(
                client, result, links, self.last_search, pdf_directory=pdf_directory
            )
        if not result.abstract and not result.full_text and not links:
            self.last_search.record_error(
                SourceParseError("article has no research text or public PDF"),
                "article content",
            )
        return result

    def _score(self, title_lower: str, terms: list[str]) -> int:
        return sum(1 for t in terms if t in title_lower)
