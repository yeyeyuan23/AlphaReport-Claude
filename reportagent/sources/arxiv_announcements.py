"""Discover public announcements in dated official lists; fetch abstracts by ID."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx

from reportagent.sources.arxiv_catalog import (
    parse_recent_index,
    parse_batch_page,
    requested_days,
)
from reportagent.sources.base import SourceParseError
from reportagent.sources.arxiv_scope import ArxivSearchScope


logger = logging.getLogger(__name__)


class AnnouncementHTTP:
    """Serialize arXiv requests, with bounded retry of transient network failures."""

    def __init__(self, client, interval):
        self.client, self.interval, self.started = client, interval, False
        self.requests = 0

    async def get(self, url, **kwargs):
        for attempt in range(3):
            if self.started and self.interval:
                await asyncio.sleep(self.interval)
            self.started = True
            try:
                self.requests += 1
                response = await self.client.get(url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                status = (
                    error.response.status_code
                    if isinstance(error, httpx.HTTPStatusError)
                    else None
                )
                if attempt == 2 or (
                    status is not None and status not in {429, 500, 502, 503, 504}
                ):
                    raise
                await asyncio.sleep(_retry_delay(error, attempt))


def _retry_delay(error, attempt):
    delay = 3 * 2**attempt
    if isinstance(error, httpx.HTTPStatusError):
        value = error.response.headers.get("Retry-After", "")
        try:
            after = (
                float(value)
                if value.isdigit()
                else (
                    parsedate_to_datetime(value) - datetime.now(timezone.utc)
                ).total_seconds()
            )
            delay = max(delay, after)
        except (ValueError, TypeError, OverflowError):
            pass
    return min(delay, 60)


async def _category_announcements(http, category, days, diagnostics):
    url = f"https://arxiv.org/list/{category}/recent"
    # This minimal page supplies all five date offsets, not a top-25 candidate cap.
    response = await http.get(url, params={"skip": 0, "show": 25})
    batches = parse_recent_index(response.content, category)
    diagnostics.successful_requests += 1
    entries = []
    for day in days:
        batch = next((b for b in batches if b.day == day), None)
        if batch is None and (
            day < batches[-1].day or (day > batches[0].day and day.weekday() < 5)
        ):
            diagnostics.record_error(
                SourceParseError(
                    "requested announcement date unavailable in recent lists; coverage incomplete"
                ),
                f"announcements {category}",
            )
            continue
        if batch and batch.count:
            try:
                entries.extend(
                    await _batch_entries(
                        http, url, category, batch, batches, diagnostics
                    )
                )
            except Exception as error:
                diagnostics.record_error(error, f"announcements {category} {day}")
    return entries


async def _batch_entries(http, url, category, batch, snapshot, diagnostics):
    entries, seen = [], set()
    skip = batch.offset
    while skip < batch.offset + batch.count:
        try:
            response = await http.get(url, params={"skip": skip, "show": 2000})
            if parse_recent_index(response.content, category) != snapshot:
                raise SourceParseError(
                    "announcement index changed during pagination; retry required"
                )
            rows = parse_batch_page(response.content, batch, skip, 2000)
            if any(identifier in seen for identifier, _ in rows):
                raise SourceParseError(
                    "announcement pagination repeated paper identities"
                )
        except Exception as error:
            diagnostics.record_error(error, f"announcements {category} {batch.day}")
            break
        seen.update(identifier for identifier, _ in rows)
        diagnostics.fetched += len(rows)
        diagnostics.successful_requests += 1
        entries.extend(
            (identifier, batch, category, kind, str(response.url))
            for identifier, kind in rows
        )
        skip += len(rows)
    return entries


async def fetch_announcements(
    criteria,
    *,
    diagnostics,
    partial_results,
    rate_limit_seconds,
    scope=None,
    stats=None,
):
    scope = scope or ArxivSearchScope.from_config()
    stats = stats if stats is not None else {}
    stats.update(
        categories={},
        unique_announcements=0,
        metadata_loaded=0,
        selected=0,
        added_by_topics=0,
        by_topic_group={group.name: 0 for group in scope.topic_groups},
        selected_by_category={category: 0 for category in scope.categories},
    )
    days = list(requested_days(criteria))
    timeout = httpx.Timeout(30.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        http = AnnouncementHTTP(client, rate_limit_seconds)
        found, listed = {}, {}
        for category in scope.categories:
            try:
                rows = await _category_announcements(http, category, days, diagnostics)
                stats["categories"][category] = len(rows)
                for entry in rows:
                    listed.setdefault(entry[0], []).append(category)
                    previous = found.get(entry[0])
                    if previous is None or previous[1].day < entry[1].day:
                        found[entry[0]] = entry
                logger.info(
                    "arXiv category=%s listed=%d unique_so_far=%d",
                    category,
                    len(rows),
                    len(found),
                )
            except Exception as error:
                stats["categories"][category] = None
                diagnostics.record_error(error, f"announcements {category}")
        stats["unique_announcements"] = len(found)
        candidates = []
        await _fetch_abstracts(http, list(found.values()), candidates, diagnostics)
        stats["metadata_loaded"] = len(candidates)
        for paper in candidates:
            identifier = re.sub(r"v\d+$", "", paper.arxiv_id or "")
            categories = list(dict.fromkeys(listed[identifier]))
            decision = scope.match(categories, paper.title, paper.abstract)
            if not decision:
                continue
            paper.raw_metadata["publication_date_info"]["listed_categories"] = (
                categories
            )
            paper.raw_metadata["arxiv_search_scope"] = decision
            partial_results.append(paper)
            for category in categories:
                stats["selected_by_category"][category] = (
                    stats["selected_by_category"].get(category, 0) + 1
                )
            stats["selected"] += 1
            stats["added_by_topics"] += int(not decision["full_categories"])
            for name in decision["topic_groups"]:
                stats["by_topic_group"][name] = stats["by_topic_group"].get(name, 0) + 1
        stats["http_requests"] = http.requests
    return partial_results


async def _fetch_abstracts(http, entries, results, diagnostics):
    from reportagent.sources.arxiv_source import ArxivSource
    from reportagent.sources.arxiv_metadata import read_abstract_page

    for offset in range(0, len(entries), 100):
        chunk = entries[offset : offset + 100]
        logger.info(
            "arXiv abstract batch=%d/%d papers=%d",
            offset // 100 + 1,
            (len(entries) + 99) // 100,
            len(chunk),
        )
        expected = {entry[0]: entry for entry in chunk}
        papers = {}
        try:
            response = await http.get(
                "https://export.arxiv.org/api/query",
                params={
                    "id_list": ",".join(expected),
                    "max_results": len(expected),
                },
            )
            papers = _parse_abstract_batch(
                response.content, expected, ArxivSource._parse_atom_entry
            )
        except Exception as error:
            logger.info(
                "arXiv abstract batch unavailable (%s); using official abstract pages",
                type(error).__name__,
            )
        for identifier, entry in expected.items():
            try:
                paper = papers.get(identifier) or await read_abstract_page(
                    http, identifier
                )
                results.append(_with_announcement(paper, entry))
            except Exception as error:
                diagnostics.record_error(error, f"announcement abstract {identifier}")


def _parse_abstract_batch(content, expected, parse_entry):
    atom = "{http://www.w3.org/2005/Atom}"
    if b"<!DOCTYPE" in content.upper():
        raise SourceParseError("unexpected abstract response document type")
    root = ET.fromstring(content)
    if root.tag != atom + "feed":
        raise SourceParseError("non-Atom abstract response")
    papers = {}
    for entry in root.findall(atom + "entry"):
        try:
            paper = parse_entry(entry)
            identifier = re.sub(r"v\d+$", "", paper.arxiv_id or "")
            if (
                identifier in expected
                and re.search(r"v[1-9]\d*$", paper.arxiv_id)
                and paper.abstract.strip()
                and paper.authors
            ):
                papers[identifier] = paper
        except (ValueError, TypeError):
            continue
    return papers


def _with_announcement(paper, entry):
    identifier, batch, category, kind, url = entry
    return paper.model_copy(
        update={
            "published_date": batch.day,
            "raw_metadata": {
                **paper.raw_metadata,
                "publication_date_info": {
                    "precision": "day",
                    "year": batch.day.year,
                    "month": batch.day.month,
                    "day": batch.day.day,
                    "evidence": batch.day.isoformat(),
                    "source": "arxiv_dated_listing",
                    "listing_date": batch.day.isoformat(),
                    "category": category,
                    "announce_type": kind,
                    "listing_url": url,
                    "metadata_version": paper.arxiv_id,
                    "source_id": identifier,
                },
            },
        }
    )
