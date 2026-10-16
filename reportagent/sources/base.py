from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import re
from urllib.parse import urlsplit, urlunsplit

from reportagent.models.schemas import UserCriteria, SearchResult, SourceType


class SourceParseError(ValueError):
    """An upstream response is not a recognized document (use a static reason)."""


def safe_source_error(error: Exception, stage: str = "search") -> str:
    """Keep useful failure categories without copying URLs, tokens or response bodies."""
    if isinstance(error, SourceParseError):
        detail = str(error)  # Reasons are static strings defined by our adapters.
    else:
        detail = type(error).__name__
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None) or getattr(error, "status", None)
        if isinstance(status, int):
            detail += f" (HTTP {status})"
    return f"{stage}: {detail}"


@dataclass
class SearchDiagnostics:
    """Per-call observations; availability alone never increments successful_requests.

    fetched counts raw candidates before source filtering/deduplication. The collection
    service counts valid/full_text after its quant filter. Errors contain safe summaries.
    """

    fetched: int = 0
    successful_requests: int = 0
    errors: list[str] = field(default_factory=list)
    unavailable: bool = False

    def record_error(self, error: Exception, stage: str = "search") -> str:
        message = safe_source_error(error, stage)
        self.errors.append(message)
        return message


def usable_full_text(text: str | None) -> bool:
    """Reject empty/HTML error payloads; content coverage is assessed downstream."""
    if not text or len(text.strip()) < 100:
        return False
    return not re.search(r"<(?:!doctype\s+html|html|body)\b", text[:1000], re.I)


def canonical_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    # Fragments identify positions in a document, not different documents. Keep the
    # query and path intact because either may identify a different report.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
                       parsed.query, ""))


def deduplicate_results(results: list[SearchResult]) -> list[SearchResult]:
    """Deduplicate known identities only; identical titles are not evidence of identity."""
    unique: list[SearchResult] = []
    seen: dict[tuple[str, str], int] = {}
    for result in results:
        key = None
        if result.arxiv_id:
            key = ("arxiv", result.arxiv_id.strip().removeprefix("arXiv:"))
        elif result.doi:
            key = ("doi", re.sub(r"^https?://(?:dx\.)?doi\.org/", "", result.doi.strip(), flags=re.I).lower())
        elif result.source_url:
            key = ("url", canonical_url(result.source_url))
        elif result.pdf_path:
            key = ("file", result.pdf_path)
        if key is None or key not in seen:
            if key:
                seen[key] = len(unique)
            unique.append(result)
        elif usable_full_text(result.full_text) and not usable_full_text(unique[seen[key]].full_text):
            unique[seen[key]] = result
    return unique


class BaseSource(ABC):
    @property
    def last_search(self) -> SearchDiagnostics:
        if not hasattr(self, "_last_search"):
            self._last_search = SearchDiagnostics()
        return self._last_search

    def _start_search(self) -> SearchDiagnostics:
        self._last_search = SearchDiagnostics()
        return self._last_search

    @abstractmethod
    async def search(self, criteria: UserCriteria) -> list[SearchResult]:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Local configuration prerequisite, not a network health check."""
        ...

    @property
    @abstractmethod
    def source_type(self) -> SourceType:
        ...
