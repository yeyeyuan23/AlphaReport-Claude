"""Parse arXiv's dated recent-announcement lists, independently of RSS rollover."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from reportagent.sources.base import SourceParseError
from reportagent.sources.date_window import in_criteria_window, utc_datetime


EASTERN = ZoneInfo("America/New_York")
IDENTIFIER = r"(?:\d{4}\.\d{4,5}|[a-z][a-z.\-]*/\d{7})"


@dataclass(frozen=True)
class AnnouncementBatch:
    day: date
    offset: int
    count: int

    @property
    def released_at(self):
        # Listing/mail dates follow the previous evening's public release.
        # https://info.arxiv.org/help/availability.html#announcement-schedule
        evening = datetime.combine(self.day - timedelta(days=1), time(20), EASTERN)
        return evening.astimezone(timezone.utc)


def requested_days(criteria):
    if not criteria.date_from or not criteria.date_to:
        raise SourceParseError("announcement search requires a bounded date window")
    start, end = utc_datetime(criteria.date_from), utc_datetime(criteria.date_to)
    if start >= end:
        raise SourceParseError("announcement search requires start before end")
    day = start.astimezone(EASTERN).date()
    last = end.astimezone(EASTERN).date() + timedelta(days=1)
    while day <= last:
        batch = AnnouncementBatch(day, 0, 0)
        if day.weekday() < 5 and in_criteria_window(batch.released_at, criteria):
            yield day
        day += timedelta(days=1)


def parse_recent_index(content, category):
    soup = BeautifulSoup(content, "html.parser")
    root = soup.select_one("#dlpage")
    paging = root.select_one(".paging") if root else None
    total = re.search(r"Total of ([\d,]+) entries", paging.get_text() if paging else "")
    links = root.select(":scope > ul > li > a") if root else []
    if not total or not links:
        raise SourceParseError("announcement list missing dated index or total")
    entries = [_index_entry(a, category) for a in links]
    count = int(total[1].replace(",", ""))
    ends = [offset for _, offset in entries[1:]] + [count]
    batches = [
        AnnouncementBatch(day, offset, end - offset)
        for (day, offset), end in zip(entries, ends)
    ]
    if (
        entries[0][1] != 0
        or any(b.count < 0 for b in batches)
        or any(a.day <= b.day for a, b in zip(batches, batches[1:]))
    ):
        raise SourceParseError("announcement list has inconsistent dates or offsets")
    return batches


def _index_entry(link, category):
    try:
        day = datetime.strptime(link.get_text(" ", strip=True), "%a, %d %b %Y").date()
        url = urlsplit(link.get("href", ""))
        if (
            url.netloc not in {"", "arxiv.org"}
            or url.path != f"/list/{category}/recent"
        ):
            raise ValueError
        offset = int(parse_qs(url.query)["skip"][0])
        if offset < 0:
            raise ValueError
        return day, offset
    except (ValueError, KeyError) as error:
        raise SourceParseError("announcement list has an invalid date index") from error


def parse_batch_page(content, batch, skip, show):
    """Only take the requested dated section, even if later sections share a page."""
    soup = BeautifulSoup(content, "html.parser")
    articles = soup.select_one("dl#articles")
    heading = articles.find("h3") if articles else None
    text = heading.get_text(" ", strip=True) if heading else ""
    date_text = text.split(" (", 1)[0]
    try:
        actual_day = datetime.strptime(date_text, "%a, %d %b %Y").date()
    except ValueError as error:
        raise SourceParseError("announcement page missing dated section") from error
    counts = re.search(r"(\d+) of (\d+) entries", text)
    expected = min(show, batch.offset + batch.count - skip)
    if (
        actual_day != batch.day
        or not counts
        or tuple(map(int, counts.groups())) != (expected, batch.count)
    ):
        raise SourceParseError(
            "announcement section count/date changed during pagination"
        )
    rows = []
    for element in heading.next_siblings:
        if getattr(element, "name", None) == "h3":
            break
        if getattr(element, "name", None) == "dt":
            rows.append(_listed_paper(element))
    if len(rows) != expected or len({row[0] for row in rows}) != len(rows):
        raise SourceParseError(
            "announcement page missing or repeating expected entries"
        )
    return rows


def _listed_paper(element):
    link = element.select_one('a[title="Abstract"]')
    match = re.fullmatch(
        r"/abs/(" + IDENTIFIER + r")", link.get("href", "") if link else ""
    )
    if not match or link.get("id") != match[1]:
        raise SourceParseError("announcement entry missing consistent arXiv identity")
    return match[1], "cross" if "cross-list from" in element.get_text() else "new"
