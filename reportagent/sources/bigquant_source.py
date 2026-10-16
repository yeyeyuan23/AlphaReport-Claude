from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import re
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from reportagent.models.schemas import UserCriteria, SearchResult, SourceType
from reportagent.sources.base import BaseSource, SourceParseError, canonical_url
from reportagent.utils.topic_expansion import expand_topics
from reportagent.sources.date_window import in_date_window
from reportagent.sources.base import usable_full_text
from reportagent.sources.content_fetch import (
    MAX_HTML_BYTES, article_text, bounded_download, hydrate_pdf, meaningful_excerpt, pdf_links,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://bigquant.com"
COLLECTION_PATH = "/wiki/collection/dLopDVBB53"
DEFAULT_MAX_PAGES = 10
DEFAULT_RATE_LIMIT = 2.0

AUTHOR_PATTERN = re.compile(r"由(.+?)创建")
DATE_TEXT = r"\d{4}\s*(?:年|[-/])\s*\d{1,2}\s*(?:月|[-/])\s*\d{1,2}\s*日?"


def _parse_publication_date(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if re.fullmatch(DATE_TEXT, value):
            stamp = datetime(*(int(n) for n in re.findall(r"\d+", value)))
        else:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # SQLite stores naive datetimes; normalize explicit offsets before saving.
        return stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo else stamp
    except ValueError:
        return None


def _publication_date(soup: BeautifulSoup, article, page_url: str = "") -> tuple[Optional[datetime], Optional[str]]:
    """Prefer the cited original report; never use page modification or sample dates."""
    text = article.get_text(" ", strip=True) if article else ""
    text = re.split(r"参考文献|引用文献|\bReferences\b", text, maxsplit=1, flags=re.I)[0]
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
            stamp = _parse_publication_date(match["date"])
            if stamp is not None:
                return stamp, "original_report_text"
    if original_date_found:
        # Invalid original-report evidence cannot be repaired by silently using
        # the repost page's publication date instead.
        return None, None

    for name in ("citation_publication_date", "citation_date", "article:published_time", "datePublished"):
        for tag in soup.find_all("meta"):
            if (tag.get("name") or tag.get("property") or "").lower() == name.lower():
                stamp = _parse_publication_date(tag.get("content"))
                if stamp is not None:
                    return stamp, f"meta:{name}"

    # Only read an Article/WebPage object's own datePublished, not dates from its
    # comments, cited references, or dateModified fields.
    def objects(value):
        if isinstance(value, list):
            for item in value:
                yield from objects(item)
        elif isinstance(value, dict):
            kind = value.get("@type", [])
            kinds = ({kind} if isinstance(kind, str) else
                     {k for k in kind if isinstance(k, str)} if isinstance(kind, list) else set())
            if kinds & {"Article", "BlogPosting", "NewsArticle", "ScholarlyArticle", "WebPage"}:
                yield value
            yield from objects(value.get("@graph", []))

    candidates = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for item in objects(data):
            identifiers = [item[key] for key in ("url", "@id") if isinstance(item.get(key), str)]
            if page_url and identifiers and not any(
                canonical_url(urljoin(page_url, value)) == canonical_url(page_url) for value in identifiers
            ):
                continue
            stamp = _parse_publication_date(item.get("datePublished"))
            if stamp is not None:
                candidates.append((bool(identifiers and page_url), stamp))
    if candidates:
        matching_page = [stamp for identified, stamp in candidates if identified]
        dates = set(matching_page or [stamp for _, stamp in candidates])
        if len(dates) == 1:
            return dates.pop(), "jsonld:datePublished"

    for tag in article.find_all("time") if article else []:
        scope = tag.find_parent(attrs={"itemscope": True})
        if scope and scope.get("itemtype") and not re.search(
            r"(?:^|[/#])(?:Article|BlogPosting|NewsArticle|ScholarlyArticle|WebPage)$", scope["itemtype"]
        ):
            continue
        if "datePublished" in (tag.get("itemprop") or "").split():
            stamp = _parse_publication_date(tag.get("datetime") or tag.get_text(strip=True))
            if stamp is not None:
                return stamp, "time:datePublished"
    return None, None


class BigQuantSource(BaseSource):
    def __init__(
        self,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT,
        max_listing_pages: int = DEFAULT_MAX_PAGES,
    ):
        self.rate_limit_seconds = rate_limit_seconds
        self.max_listing_pages = max_listing_pages

    @property
    def source_type(self) -> SourceType:
        return SourceType.BIGQUANT

    def is_available(self) -> bool:
        return True

    async def hydrate_result(self, result: SearchResult) -> SearchResult:
        """Re-fetch one public article's evidence without models or database access."""
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            return await self._fetch_detail(client, result.title, result.source_url or result.raw_metadata["doc_path"])

    async def search(self, criteria: UserCriteria) -> list[SearchResult]:
        self._start_search()
        expanded = expand_topics(criteria.topics, criteria.keywords, lang="all")
        terms = [t.lower() for t in expanded]
        candidates: list[tuple[str, str, int]] = []
        seen_paths: set[str] = set()

        async with httpx.AsyncClient(timeout=20.0) as client:
            for page_no in range(1, self.max_listing_pages + 1):
                try:
                    entries = await self._fetch_listing_page(client, page_no)
                except Exception as e:
                    logger.warning("BigQuant %s", self.last_search.record_error(e, f"listing page {page_no}"))
                    break

                self.last_search.successful_requests += 1
                self.last_search.fetched += len(entries)
                if not entries:
                    break

                for title, doc_path in entries:
                    key = canonical_url(f"{BASE_URL}{doc_path}" if doc_path.startswith("/") else doc_path)
                    if key in seen_paths:
                        continue
                    seen_paths.add(key)
                    score = self._score(title.lower(), terms)
                    if score > 0 or not terms:
                        candidates.append((title, doc_path, score))

                await asyncio.sleep(self.rate_limit_seconds)

            candidates.sort(key=lambda x: x[2], reverse=True)
            results: list[SearchResult] = []
            for title, doc_path, _ in candidates:
                try:
                    sr = await self._fetch_detail(client, title, doc_path)
                    if sr and in_date_window(sr.published_date, criteria.date_from, criteria.date_to):
                        results.append(sr)
                    if len(results) >= criteria.max_results_per_source:
                        break
                    await asyncio.sleep(self.rate_limit_seconds)
                except Exception as e:
                    logger.warning("BigQuant %s", self.last_search.record_error(e, "article detail"))

        logger.info(
            "BigQuant: scanned %d titles, matched %d, fetched %d details",
            self.last_search.fetched,
            len(candidates),
            len(results),
        )
        return results

    async def _fetch_listing_page(
        self, client: httpx.AsyncClient, page_no: int
    ) -> list[tuple[str, str]]:
        url = f"{BASE_URL}{COLLECTION_PATH}?page={page_no}"
        body = await bounded_download(client, url, MAX_HTML_BYTES)
        soup = BeautifulSoup(body, "html.parser")
        entries: list[tuple[str, str]] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/wiki/doc/" in href:
                title = a.get_text(strip=True)
                if title:
                    entries.append((title, href))
        if not entries:
            # An arbitrary login/challenge/error page must not be called an empty
            # collection. Accept only an explicit empty-state element or message.
            empty = soup.select_one(".ant-empty, .empty-state, [data-testid='empty']")
            text = soup.get_text(" ", strip=True).lower()
            empty_message = any(marker in text for marker in (
                "暂无内容", "暂无文档", "暂无文章", "no documents", "no results"))
            if not empty and not (soup.find("h1") and empty_message):
                raise SourceParseError("unrecognized HTML collection page")
        return entries

    async def _fetch_detail(
        self, client: httpx.AsyncClient, title: str, doc_path: str
    ) -> Optional[SearchResult]:
        url = f"{BASE_URL}{doc_path}" if doc_path.startswith("/") else doc_path
        body = await bounded_download(client, url, MAX_HTML_BYTES)
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

        text = article_text(article)
        abstract = (text[:1500] if text else None) or abstract
        links = pdf_links(article, url)

        source_url = f"{BASE_URL}{doc_path}" if doc_path.startswith("/") else doc_path
        published_date, date_source = _publication_date(soup, article, source_url)

        result = SearchResult(
            title=title,
            authors=authors,
            abstract=abstract,
            full_text=text if usable_full_text(text) else None,
            abstract_only=not usable_full_text(text),
            source=SourceType.BIGQUANT,
            source_url=source_url,
            published_date=published_date,
            raw_metadata={
                "doc_path": doc_path,
                "pdf_link": links[0] if links else None,
                "published_date_source": date_source,
                "content_kind": "html" if text else "metadata_only",
                "text_scope": "html_article" if text else "metadata_only",
            },
        )
        if links:
            result = await hydrate_pdf(client, result, links, self.last_search)
        if not result.abstract and not result.full_text and not links:
            self.last_search.record_error(SourceParseError("article has no research text or public PDF"), "article content")
        return result

    def _score(self, title_lower: str, terms: list[str]) -> int:
        return sum(1 for t in terms if t in title_lower)
