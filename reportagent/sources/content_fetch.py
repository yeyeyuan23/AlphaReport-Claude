"""Bounded public downloads and verbatim research text extraction.

HTML previews remain source excerpts. They are not model-written summaries and do
not establish that every page of an attached report is available.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from reportagent.models.schemas import SearchResult
from reportagent.sources.base import (
    SearchDiagnostics,
    SourceParseError,
    usable_full_text,
)
from reportagent.utils.config import PROJECT_ROOT, get_config

logger = logging.getLogger(__name__)
PDF_DIRECTORY = PROJECT_ROOT / "data" / "pdfs"
MAX_HTML_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class PDFPolicy:
    max_size_mb: float = 25
    download_timeout_seconds: float = 45
    parse_timeout_seconds: float = 20
    max_attempts: int = 3
    retry_backoff_seconds: float = 2
    max_retry_delay_seconds: float = 30

    @property
    def max_bytes(self) -> int:
        return int(self.max_size_mb * 1024 * 1024)


def get_pdf_policy() -> PDFPolicy:
    values = get_config("collection", "pdf", default={})
    if not isinstance(values, dict):
        raise ValueError("collection.pdf must be a mapping")
    defaults = PDFPolicy()
    selected = {
        key: values.get(key, getattr(defaults, key))
        for key in defaults.__dataclass_fields__
    }
    for key, value in selected.items():
        if key == "max_attempts":
            valid = type(value) is int and value >= 1
        else:
            valid = (
                type(value) in (int, float)
                and math.isfinite(value)
                and (value >= 0 if key == "retry_backoff_seconds" else value > 0)
            )
        if not valid:
            raise ValueError(f"collection.pdf.{key} has an invalid value")
    policy = PDFPolicy(**selected)
    if policy.max_bytes < 1:
        raise ValueError("collection.pdf.max_size_mb must allow at least one byte")
    return policy


def paper_identifier(result: SearchResult) -> str:
    if result.arxiv_id and re.fullmatch(r"[\w./-]{1,100}", result.arxiv_id):
        return result.arxiv_id
    # Do not log signed attachment URLs or arbitrary upstream metadata.
    return hashlib.sha256(
        (result.source_url or result.doi or result.title).encode()
    ).hexdigest()[:16]


class PDFTransferError(SourceParseError):
    def __init__(
        self, stage, reason, *, source, paper_id, attempt, byte_count, elapsed_ms
    ):
        self.stage = stage
        super().__init__(
            f"{stage} [source={source}, paper={paper_id}, attempt={attempt}, "
            f"bytes={byte_count}, elapsed_ms={elapsed_ms}]: {reason}"
        )


def _event(
    stage, status, source, paper_id, started, byte_count, *, attempt=1, **details
):
    elapsed = round((time.monotonic() - started) * 1000)
    fields = dict(
        stage=stage,
        status=status,
        source=source,
        paper_id=paper_id,
        bytes=byte_count,
        elapsed_ms=elapsed,
        attempt=attempt,
        **details,
    )
    logger.log(
        logging.INFO if status == "ok" else logging.WARNING,
        "pdf_transfer %s",
        json.dumps(fields, ensure_ascii=False),
    )
    return elapsed


def _safe_reason(error):
    if isinstance(error, SourceParseError):
        return str(error)
    status = (
        error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
    )
    return type(error).__name__ + (f" (HTTP {status})" if status else "")


def _retry_delay(error, attempt, policy):
    delay = min(
        policy.max_retry_delay_seconds,
        policy.retry_backoff_seconds * 2 ** min(attempt - 1, 20),
    )
    if isinstance(error, httpx.HTTPStatusError):
        header = error.response.headers.get("Retry-After", "")
        try:
            after = float(header)
        except ValueError:
            try:
                after = (
                    parsedate_to_datetime(header) - datetime.now(timezone.utc)
                ).total_seconds()
            except (TypeError, ValueError, OverflowError):
                after = 0
        if math.isfinite(after):
            delay = min(policy.max_retry_delay_seconds, max(delay, after))
    return delay


async def bounded_download(
    client,
    url: str,
    max_bytes: int,
    *,
    policy: PDFPolicy | None = None,
    source="public",
    paper_id: str | None = None,
) -> bytes:
    """Bound each attempt, discard partial bytes on retry; HTML retains one attempt."""
    request_options = (
        {"timeout": httpx.Timeout(policy.download_timeout_seconds)} if policy else {}
    )
    policy = policy or PDFPolicy(max_attempts=1)
    paper_id = paper_id or hashlib.sha256(url.encode()).hexdigest()[:16]
    if urlsplit(url).scheme not in {"https", "http"}:
        raise SourceParseError("unsupported public download URL")
    for attempt in range(1, policy.max_attempts + 1):
        started, byte_count = time.monotonic(), 0

        async def read():
            nonlocal byte_count
            async with client.stream(
                "GET", url, follow_redirects=True, **request_options
            ) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise SourceParseError(
                        f"download exceeds size limit (declared_bytes={length}, max_bytes={max_bytes})"
                    )
                chunks = []
                async for chunk in response.aiter_bytes():
                    byte_count += len(chunk)
                    if byte_count > max_bytes:
                        raise SourceParseError(
                            f"download exceeds size limit (max_bytes={max_bytes})"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)

        try:
            data = await asyncio.wait_for(
                read(), timeout=policy.download_timeout_seconds
            )
            _event(
                "download", "ok", source, paper_id, started, byte_count, attempt=attempt
            )
            return data
        except Exception as error:
            reason = _safe_reason(error)
            retryable = isinstance(
                error,
                (
                    TimeoutError,
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    httpx.RemoteProtocolError,
                ),
            )
            retryable |= isinstance(
                error, httpx.HTTPStatusError
            ) and error.response.status_code in {408, 429, 500, 502, 503, 504}
            retry = retryable and attempt < policy.max_attempts
            delay = _retry_delay(error, attempt, policy) if retry else 0
            elapsed = _event(
                "download",
                "retry" if retry else "failed",
                source,
                paper_id,
                started,
                byte_count,
                attempt=attempt,
                reason=reason,
                timeout_seconds=policy.download_timeout_seconds,
                max_bytes=max_bytes,
                retry_delay_seconds=delay,
            )
            if not retry:
                raise PDFTransferError(
                    "download",
                    reason,
                    source=source,
                    paper_id=paper_id,
                    attempt=attempt,
                    byte_count=byte_count,
                    elapsed_ms=elapsed,
                ) from error
            await asyncio.sleep(delay)


async def pdf_stage(
    stage,
    function,
    *args,
    policy: PDFPolicy,
    source,
    paper_id,
    byte_count,
    diagnostics: SearchDiagnostics | None = None,
):
    """Time local work separately; parsing never consumes the download deadline."""
    started = time.monotonic()
    try:
        work = asyncio.to_thread(function, *args)
        result = (
            await asyncio.wait_for(work, timeout=policy.parse_timeout_seconds)
            if stage == "parse"
            else await work
        )
        if stage == "parse" and not usable_full_text(result):
            reason = "PDF has no usable extracted text"
            elapsed = _event(
                stage, "empty", source, paper_id, started, byte_count, reason=reason
            )
            if diagnostics is not None:
                diagnostics.record_error(
                    PDFTransferError(
                        stage,
                        reason,
                        source=source,
                        paper_id=paper_id,
                        attempt=1,
                        byte_count=byte_count,
                        elapsed_ms=elapsed,
                    ),
                    "PDF parse",
                )
            return None
        if stage == "save" and not result:
            raise SourceParseError("PDF could not be saved")
        _event(stage, "ok", source, paper_id, started, byte_count)
        return result
    except Exception as error:
        reason = _safe_reason(error)
        elapsed = _event(
            stage,
            "failed",
            source,
            paper_id,
            started,
            byte_count,
            reason=reason,
            timeout_seconds=policy.parse_timeout_seconds if stage == "parse" else None,
        )
        raise PDFTransferError(
            stage,
            reason,
            source=source,
            paper_id=paper_id,
            attempt=1,
            byte_count=byte_count,
            elapsed_ms=elapsed,
        ) from error


def validate_pdf_header(data: bytes, *, source, paper_id):
    if not data[:1024].lstrip().startswith(b"%PDF-"):
        reason = "non-PDF response content"
        elapsed = _event(
            "validate",
            "failed",
            source,
            paper_id,
            time.monotonic(),
            len(data),
            reason=reason,
        )
        raise PDFTransferError(
            "validate",
            reason,
            source=source,
            paper_id=paper_id,
            attempt=1,
            byte_count=len(data),
            elapsed_ms=elapsed,
        )


def meaningful_excerpt(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if len(re.sub(r"[\W_]+", "", text, flags=re.UNICODE)) < 20:
        return None
    if re.search(r"<(?:html|body|!doctype)\b", text[:500], re.I):
        return None
    return text


def article_text(article) -> str | None:
    if article is None:
        return None
    # Copy so dates/authors can still be read from the source tree.
    copy = BeautifulSoup(str(article), "html.parser")
    for tag in copy.select(
        "script, style, nav, aside, header, footer, h1, time, .comment, .comments"
    ):
        tag.decompose()
    for link in copy.find_all("a", href=True):
        href = link["href"]
        if ".pdf" in href.lower() or link.get_text(strip=True) == href:
            link.decompose()
    lines = []
    for line in copy.get_text("\n", strip=True).splitlines():
        line = line.strip()
        if not line or re.match(r"^(?:由.+创建|更新于|被浏览\s*\d+|\d+\s*用户$)", line):
            continue
        if re.sub(r"[\W_]+", "", line, flags=re.UNICODE):
            lines.append(line)
    return meaningful_excerpt("\n".join(lines))


def pdf_links(soup, page_url: str) -> list[str]:
    urls = []
    for tag in soup.find_all(["a", "iframe", "embed"], href=True) + soup.find_all(
        ["iframe", "embed"], src=True
    ):
        href = tag.get("href") or tag.get("src") or ""
        if ".pdf" in urlsplit(href).path.lower():
            url = urljoin(page_url, href)
            if urlsplit(url).scheme in {"http", "https"} and url not in urls:
                urls.append(url)
    return urls


def _extract_pdf_text(data: bytes) -> str:
    from reportagent.processors.pdf_extractor import PDFExtractor

    return PDFExtractor().extract_bytes(data).text


def _save_public_pdf(data: bytes, source: str, *, directory: Path | None = None) -> str:
    pdf_directory = Path(directory if directory is not None else PDF_DIRECTORY)
    pdf_directory.mkdir(parents=True, exist_ok=True)
    path = pdf_directory / f"{source}-{hashlib.sha256(data).hexdigest()[:24]}.pdf"
    path.write_bytes(data)
    return str(path)


async def hydrate_pdf(
    client,
    result: SearchResult,
    urls: list[str],
    diagnostics: SearchDiagnostics,
    *,
    pdf_directory: Path | None = None,
) -> SearchResult:
    policy = get_pdf_policy()
    identity, source = paper_identifier(result), result.source.value
    for url in urls[:3]:
        try:
            data = await bounded_download(
                client,
                url,
                policy.max_bytes,
                policy=policy,
                source=source,
                paper_id=identity,
            )
            validate_pdf_header(data, source=source, paper_id=identity)
            text = await pdf_stage(
                "parse",
                _extract_pdf_text,
                data,
                policy=policy,
                source=source,
                paper_id=identity,
                byte_count=len(data),
                diagnostics=diagnostics,
            )
            save = (
                partial(_save_public_pdf, directory=pdf_directory)
                if pdf_directory is not None
                else _save_public_pdf
            )
            path = await pdf_stage(
                "save",
                save,
                data,
                source,
                policy=policy,
                source=source,
                paper_id=identity,
                byte_count=len(data),
            )
            updates = {
                "pdf_path": path,
                "raw_metadata": {
                    **result.raw_metadata,
                    "pdf_link": url,
                    "content_kind": "pdf"
                    if text
                    else result.raw_metadata.get("content_kind", "pdf_without_text"),
                    "text_scope": "linked_pdf"
                    if text
                    else result.raw_metadata.get("text_scope", "metadata_only"),
                },
            }
            if text:
                updates.update(
                    full_text=text,
                    abstract_only=False,
                    abstract=result.abstract or text[:1500].strip(),
                )
            result = result.model_copy(update=updates)
            if text:
                break
        except Exception as error:
            diagnostics.record_error(
                error,
                f"PDF {error.stage}"
                if isinstance(error, PDFTransferError)
                else "PDF download/parse",
            )
    if urls and result.raw_metadata.get("text_scope") == "html_article":
        # Preserve the visible webpage evidence, but do not persist its preview
        # as a successfully acquired underlying report when the PDF failed.
        result = result.model_copy(update={"abstract_only": True})
    return result
