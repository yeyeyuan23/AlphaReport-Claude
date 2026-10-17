"""Read official arXiv abstracts when the batch query API is unavailable."""

from datetime import datetime, timezone
import re

from bs4 import BeautifulSoup

from reportagent.models.schemas import SearchResult, SourceType
from reportagent.sources.base import SourceParseError
from reportagent.sources.arxiv_catalog import IDENTIFIER


async def read_abstract_page(http, identifier):
    response = await http.get(f"https://arxiv.org/abs/{identifier}")
    soup = BeautifulSoup(response.content, "html.parser")
    version = _page_version(soup, identifier)
    title = _meta(soup, "citation_title")
    identity = _meta(soup, "citation_arxiv_id")
    body = soup.select_one("blockquote.abstract")
    history = soup.select_one(".submission-history")
    authors = [
        tag.get("content", "").strip()
        for tag in soup.select('meta[name="citation_author"]')
    ]
    if (
        re.sub(r"v\d+$", "", identity) != re.sub(r"v\d+$", "", identifier)
        or not title
        or not body
        or not history
        or not authors
        or not all(authors)
    ):
        raise SourceParseError(
            "official abstract missing consistent identity or metadata"
        )
    return SearchResult(
        title=title,
        authors=authors,
        source=SourceType.ARXIV,
        arxiv_id=version,
        abstract=re.sub(r"^Abstract:\s*", "", body.get_text(" ", strip=True)),
        source_url=f"https://arxiv.org/abs/{version}",
        abstract_only=True,
        published_date=_original_submission(history),
        doi=_meta(soup, "citation_doi") or None,
        raw_metadata={"pdf_url": f"https://arxiv.org/pdf/{version}"},
    )


def _page_version(soup, identifier):
    match = re.search(
        r"arXiv:(" + IDENTIFIER + r"v[1-9]\d*)\b", soup.get_text(" ", strip=True)
    )
    if not match or re.sub(r"v\d+$", "", match[1]) != re.sub(r"v\d+$", "", identifier):
        raise SourceParseError(
            "official abstract missing consistent versioned identity"
        )
    if re.search(r"v\d+$", identifier) and match[1] != identifier:
        raise SourceParseError("official abstract returned another revision")
    return match[1]


def _original_submission(history):
    dates = re.search(
        r"\[v1\]\s*(\w{3},\s*\d{1,2} \w{3} \d{4} \d{2}:\d{2}:\d{2}) UTC",
        history.get_text(" ", strip=True),
    )
    if not dates:
        raise SourceParseError("official abstract missing original submission history")
    return datetime.strptime(dates[1], "%a, %d %b %Y %H:%M:%S").replace(
        tzinfo=timezone.utc
    )


def _meta(soup, name):
    tag = soup.find("meta", attrs={"name": name})
    return tag.get("content", "").strip() if tag else ""
