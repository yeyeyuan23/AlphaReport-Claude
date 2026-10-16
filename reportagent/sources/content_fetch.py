"""Bounded public downloads and verbatim research text extraction.

HTML previews remain source excerpts. They are not model-written summaries and do
not establish that every page of an attached report is available.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from reportagent.models.schemas import SearchResult
from reportagent.sources.base import SearchDiagnostics, SourceParseError, usable_full_text
from reportagent.utils.config import PROJECT_ROOT

PDF_DIRECTORY = PROJECT_ROOT / "data" / "pdfs"
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_HTML_BYTES = 5 * 1024 * 1024


async def bounded_download(client, url: str, max_bytes: int) -> bytes:
    """Stream a bounded response; limit actual bytes even without Content-Length."""
    async def read():
        if urlsplit(url).scheme not in {"https", "http"}:
            raise SourceParseError("unsupported public download URL")
        async with client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise SourceParseError("download exceeds size limit")
            chunks = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise SourceParseError("download exceeds size limit")
                chunks.append(chunk)
            return b"".join(chunks)
    return await asyncio.wait_for(read(), timeout=45)


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
    for tag in copy.select("script, style, nav, aside, header, footer, h1, time, .comment, .comments"):
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
    for tag in soup.find_all(["a", "iframe", "embed"], href=True) + soup.find_all(["iframe", "embed"], src=True):
        href = tag.get("href") or tag.get("src") or ""
        if ".pdf" in urlsplit(href).path.lower():
            url = urljoin(page_url, href)
            if urlsplit(url).scheme in {"http", "https"} and url not in urls:
                urls.append(url)
    return urls


def _extract_and_save_pdf(data: bytes, source: str) -> tuple[str | None, str]:
    import fitz
    with fitz.open(stream=data, filetype="pdf") as document:
        text = "\n".join(page.get_text() for page in document)
    # Opening with a PDF parser validates the file before it reaches the cache.
    PDF_DIRECTORY.mkdir(parents=True, exist_ok=True)
    path = Path(PDF_DIRECTORY) / f"{source}-{hashlib.sha256(data).hexdigest()[:24]}.pdf"
    path.write_bytes(data)
    return text if usable_full_text(text) else None, str(path)


async def hydrate_pdf(client, result: SearchResult, urls: list[str], diagnostics: SearchDiagnostics) -> SearchResult:
    for url in urls[:3]:
        try:
            data = await bounded_download(client, url, MAX_PDF_BYTES)
            if not data[:1024].lstrip().startswith(b"%PDF-"):
                raise SourceParseError("non-PDF response content")
            text, path = await asyncio.wait_for(
                asyncio.to_thread(_extract_and_save_pdf, data, result.source.value), timeout=20)
            updates = {"pdf_path": path, "raw_metadata": {**result.raw_metadata, "pdf_link": url,
                                                          "content_kind": "pdf" if text else result.raw_metadata.get("content_kind", "pdf_without_text"),
                                                          "text_scope": "linked_pdf" if text else result.raw_metadata.get("text_scope", "metadata_only")}}
            if text:
                updates.update(full_text=text, abstract_only=False,
                               abstract=result.abstract or text[:1500].strip())
            else:
                diagnostics.record_error(SourceParseError("PDF has no usable extracted text"), "PDF parse")
            result = result.model_copy(update=updates)
            if text:
                break
        except Exception as error:
            diagnostics.record_error(error, "PDF download/parse")
    if urls and result.raw_metadata.get("text_scope") == "html_article":
        # Preserve the visible webpage evidence, but do not persist its preview
        # as a successfully acquired underlying report when the PDF failed.
        result = result.model_copy(update={"abstract_only": True})
    return result
