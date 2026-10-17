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


# The q-fin archive includes all nine q-fin subcategories and their cross-lists.
ANNOUNCEMENT_CATEGORIES = ("q-fin", "cs.LG", "stat.ML", "cs.CL", "cs.AI", "math.OC")
logger = logging.getLogger(__name__)


class AnnouncementHTTP:
    """Serialize arXiv requests, with bounded retry of transient network failures."""

    def __init__(self, client, interval):
        self.client, self.interval, self.started = client, interval, False

    async def get(self, url, **kwargs):
        for attempt in range(3):
            if self.started and self.interval:
                await asyncio.sleep(self.interval)
            self.started = True
            try:
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
        if day < batches[-1].day or day > batches[0].day:
            diagnostics.record_error(
                SourceParseError(
                    "requested announcement day outside available recent lists; coverage incomplete"
                ),
                f"announcements {category}",
            )
            continue
        batch = next((b for b in batches if b.day == day), None)
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
    criteria, *, diagnostics, partial_results, rate_limit_seconds
):
    days = list(requested_days(criteria))
    timeout = httpx.Timeout(30.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        http = AnnouncementHTTP(client, rate_limit_seconds)
        found = {}
        for category in ANNOUNCEMENT_CATEGORIES:
            try:
                rows = await _category_announcements(http, category, days, diagnostics)
                for entry in rows:
                    previous = found.get(entry[0])
                    if previous is None or previous[1].day < entry[1].day:
                        found[entry[0]] = entry
            except Exception as error:
                diagnostics.record_error(error, f"announcements {category}")
        await _fetch_abstracts(http, list(found.values()), partial_results, diagnostics)
    return partial_results


async def _fetch_abstracts(http, entries, results, diagnostics):
    from reportagent.sources.arxiv_source import ArxivSource
    from reportagent.sources.arxiv_metadata import read_abstract_page

    for offset in range(0, len(entries), 100):
        chunk = entries[offset : offset + 100]
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
            "announced_at": batch.released_at,
            "raw_metadata": {
                **paper.raw_metadata,
                "announcement_info": {
                    "source": "arxiv_dated_listing",
                    "listing_date": batch.day.isoformat(),
                    "category": category,
                    "announce_type": kind,
                    "listing_url": url,
                    "release_rule": "previous calendar day 20:00 America/New_York",
                    "schedule_url": "https://info.arxiv.org/help/availability.html#announcement-schedule",
                    "metadata_version": paper.arxiv_id,
                    "source_id": identifier,
                },
            },
        }
    )
