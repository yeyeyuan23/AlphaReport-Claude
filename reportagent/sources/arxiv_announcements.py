"""Official arXiv RSS announcements; submission dates are deliberately not inferred.

RSS exposes the latest announcement batch, not a historical search API. Every
category is requested separately to avoid the combined-feed 2000 item ceiling.
See https://info.arxiv.org/help/rss_specifications.html and /help/rss.html.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import re
from xml.etree import ElementTree as ET

import httpx

from reportagent.models.schemas import SearchResult, SourceType, UserCriteria
from reportagent.sources.base import SearchDiagnostics, SourceParseError
from reportagent.sources.date_window import (
    candidate_in_window,
    in_criteria_window,
    utc_datetime,
)


ANNOUNCEMENT_CATEGORIES = (
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
)
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"
IDENTIFIER = r"(?:\d{4}\.\d{4,5}|[a-z][a-z.\-]*/\d{7})v[1-9]\d*"


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "br", "div"}:
            self.parts.append(" ")


def _text(value: str) -> str:
    parser = _Text()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())


def _rss_date(value: str | None) -> datetime:
    try:
        stamp = parsedate_to_datetime(value or "")
        if stamp.tzinfo is None:
            raise ValueError
        return utc_datetime(stamp)
    except (ValueError, TypeError, OverflowError) as error:
        raise SourceParseError(
            "RSS missing valid timezone-aware announcement pubDate"
        ) from error


def _parse_item(item: ET.Element, feed_url: str, batch_pubdate: str) -> SearchResult:
    title = _text(item.findtext("title") or "")
    description = _text(item.findtext("description") or "")
    link = (item.findtext("link") or "").strip()
    identifier = re.search(r"(?:^|\s)arXiv:(" + IDENTIFIER + r")\b", description)
    guid = (item.findtext("guid") or "").strip()
    announce_type = (item.findtext(ARXIV_NS + "announce_type") or "").strip()
    description_type = re.search(r"\bAnnounce Type:\s*(\S+)", description)
    abstract = re.search(r"\bAbstract:\s*(.+)", description)
    categories = [
        value.text.strip()
        for value in item.findall("category")
        if value.text and value.text.strip()
    ]
    if not title or not identifier or not abstract or not categories:
        raise SourceParseError(
            "RSS item missing title, versioned identifier, categories or abstract"
        )
    paper_id = identifier.group(1)
    if guid != "oai:arXiv.org:" + paper_id:
        raise SourceParseError("RSS item inconsistent or missing versioned guid")
    bare_id = re.sub(r"v\d+$", "", paper_id)
    if link not in {
        f"{scheme}://arxiv.org/abs/{value}"
        for scheme in ("http", "https")
        for value in (paper_id, bare_id)
    }:
        raise SourceParseError("RSS item missing or inconsistent arXiv abstract link")
    if (
        announce_type not in {"new", "cross", "replace", "replace-cross"}
        or not description_type
        or description_type.group(1) != announce_type
    ):
        raise SourceParseError("RSS item missing or inconsistent announcement type")
    pubdate = (item.findtext("pubDate") or "").strip()
    announced = _rss_date(pubdate)
    creators = item.findall(DC_NS + "creator")
    authors = [
        author.strip()
        for creator in creators
        for author in _text(creator.text or "").split(",")
        if author.strip()
    ]
    return SearchResult(
        title=title,
        authors=authors,
        abstract=abstract.group(1).strip(),
        abstract_only=True,
        source=SourceType.ARXIV,
        source_url=f"https://arxiv.org/abs/{paper_id}",
        arxiv_id=paper_id,
        doi=item.findtext(ARXIV_NS + "DOI"),
        published_date=None,
        announced_at=announced,
        raw_metadata={
            "categories": categories,
            "primary_category": categories[0],
            "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
            "announcement_info": {
                "source": "arxiv_rss",
                "pubDate": pubdate,
                "feed_url": feed_url,
                "batch_pubDate": batch_pubdate,
                "announce_type": announce_type,
            },
        },
    )


def parse_announcement_feed(
    content: bytes,
    *,
    feed_url: str,
    criteria: UserCriteria,
    diagnostics: SearchDiagnostics,
    stage: str,
) -> list[SearchResult]:
    """Keep valid siblings while reporting any unprovable coverage or invalid item."""
    if b"<!DOCTYPE" in content.upper():
        raise SourceParseError("unexpected XML document type")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise SourceParseError("malformed RSS XML") from error
    channel = root.find("channel")
    if root.tag != "rss" or root.get("version") != "2.0" or channel is None:
        raise SourceParseError("non-RSS 2.0 announcement response")
    pubdate = (channel.findtext("pubDate") or "").strip()
    batch = _rss_date(pubdate)
    # lastBuildDate is a generation timestamp and cannot establish announcement
    # coverage, including when a stale batch has just been regenerated.
    start, end = criteria.date_from, criteria.date_to
    if (
        not start
        or not end
        or utc_datetime(end) - utc_datetime(start) > timedelta(days=1)
        or not in_criteria_window(batch, criteria)
    ):
        diagnostics.record_error(
            SourceParseError(
                "RSS latest batch cannot establish complete requested window; historical backfill unavailable"
            ),
            stage,
        )
    items = channel.findall("item")
    diagnostics.fetched += len(items)
    if len(items) >= 2000:
        diagnostics.record_error(
            SourceParseError("RSS feed reached 2000 items; possible upstream cutoff"),
            stage,
        )
    results: list[SearchResult] = []
    seen: set[str] = set()
    valid_items = 0
    for item in items:
        try:
            result = _parse_item(item, feed_url, pubdate)
            valid_items += 1
            if result.announced_at != batch:
                diagnostics.record_error(
                    SourceParseError(
                        "RSS item announcement differs from channel batch; coverage incomplete"
                    ),
                    stage + " item",
                )
            if result.arxiv_id in seen:
                raise SourceParseError(
                    "RSS repeated item within category; coverage incomplete"
                )
            seen.add(result.arxiv_id)
            if result.raw_metadata["announcement_info"]["announce_type"] not in {
                "new",
                "cross",
            }:
                continue
            if candidate_in_window(result, criteria):
                results.append(result)
        except Exception as error:
            diagnostics.record_error(error, stage + " item")
    if valid_items or not items:
        diagnostics.successful_requests += 1
    return results


async def fetch_announcements(
    criteria: UserCriteria,
    *,
    diagnostics: SearchDiagnostics,
    partial_results: list[SearchResult],
    rate_limit_seconds: float,
) -> list[SearchResult]:
    """Fetch independently; one failed category never discards successful siblings."""
    timeout = httpx.Timeout(30.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for index, category in enumerate(ANNOUNCEMENT_CATEGORIES):
            if index and rate_limit_seconds:
                await asyncio.sleep(rate_limit_seconds)
            url = f"https://rss.arxiv.org/rss/{category}"
            stage = f"RSS {category}"
            try:
                response = await client.get(url)
                response.raise_for_status()
                partial_results.extend(
                    parse_announcement_feed(
                        response.content,
                        feed_url=url,
                        criteria=criteria,
                        diagnostics=diagnostics,
                        stage=stage,
                    )
                )
            except Exception as error:
                diagnostics.record_error(error, stage)
    return partial_results
