from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from reportagent.models.schemas import UserCriteria, SearchResult, SourceType
from reportagent.sources.base import BaseSource, SourceParseError, canonical_url
from reportagent.utils.topic_expansion import expand_topics

logger = logging.getLogger(__name__)

BASE_URL = "https://bigquant.com"
COLLECTION_PATH = "/wiki/collection/dLopDVBB53"
DEFAULT_MAX_PAGES = 10
DEFAULT_RATE_LIMIT = 2.0

AUTHOR_PATTERN = re.compile(r"由(.+?)创建")


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
            top = candidates[: criteria.max_results_per_source]

            results: list[SearchResult] = []
            for title, doc_path, _ in top:
                try:
                    sr = await self._fetch_detail(client, title, doc_path)
                    if sr:
                        results.append(sr)
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
        resp = await client.get(url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
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
        resp = await client.get(url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

        abstract = None
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            abstract = meta_desc["content"].strip()

        authors: list[str] = []
        article = soup.find("article")
        if article:
            article_text = article.get_text()
            m = AUTHOR_PATTERN.search(article_text)
            if m:
                authors = [a.strip() for a in m.group(1).split(",") if a.strip()]

            if not abstract:
                paragraphs = article.find_all("p")
                text_parts = []
                for p in paragraphs[:5]:
                    t = p.get_text(strip=True)
                    if t and len(t) > 20:
                        text_parts.append(t)
                if text_parts:
                    abstract = "\n".join(text_parts)[:500]

        if not h1 or not title.strip() or not article or not abstract:
            raise SourceParseError("unrecognized HTML article or missing content")

        pdf_link = None
        for a in soup.find_all("a", href=True):
            if ".pdf" in a["href"]:
                href = a["href"]
                pdf_link = f"{BASE_URL}{href}" if href.startswith("/") else href
                break

        source_url = f"{BASE_URL}{doc_path}" if doc_path.startswith("/") else doc_path

        return SearchResult(
            title=title,
            authors=authors,
            abstract=abstract,
            full_text=None,
            abstract_only=True,
            source=SourceType.BIGQUANT,
            source_url=source_url,
            published_date=None,
            raw_metadata={
                "doc_path": doc_path,
                "pdf_link": pdf_link,
            },
        )

    def _score(self, title_lower: str, terms: list[str]) -> int:
        return sum(1 for t in terms if t in title_lower)
