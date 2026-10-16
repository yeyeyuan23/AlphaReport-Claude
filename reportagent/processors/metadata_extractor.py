from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from reportagent.processors.pdf_extractor import PDFContent


class MetadataExtractor:
    _DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
    _ARXIV_PATTERN = re.compile(r"(\d{4}\.\d{4,5})")

    def extract(self, pdf_content: PDFContent) -> dict:
        meta = pdf_content.metadata
        first_page = pdf_content.text[:3000]

        title = meta.get("title", "").strip()
        if not title:
            title = self._extract_title_from_text(first_page)

        authors = self._parse_authors(meta.get("author", ""))

        doi = self._find_pattern(self._DOI_PATTERN, first_page)
        arxiv_id = self._find_pattern(self._ARXIV_PATTERN, first_page)

        date = self._parse_date(meta.get("creation_date", ""))

        return {
            "title": title,
            "authors": authors,
            "doi": doi,
            "arxiv_id": arxiv_id,
            "date": date,
        }

    def _extract_title_from_text(self, text: str) -> str:
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if lines:
            candidate = lines[0]
            if len(candidate) > 10:
                return candidate[:500]
            elif len(lines) > 1:
                return (candidate + " " + lines[1])[:500]
        return "Untitled"

    def _parse_authors(self, author_str: str) -> list[str]:
        if not author_str:
            return []
        for sep in [";", " and ", ","]:
            if sep in author_str:
                return [a.strip() for a in author_str.split(sep) if a.strip()]
        return [author_str.strip()] if author_str.strip() else []

    def _find_pattern(self, pattern: re.Pattern, text: str) -> Optional[str]:
        match = pattern.search(text)
        return match.group(0) if match else None

    def _parse_date(self, date_str: str) -> Optional[datetime]:
        if not date_str:
            return None
        date_str = date_str.replace("D:", "").strip()
        for fmt in ("%Y%m%d%H%M%S", "%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(date_str[:len(fmt.replace("%", "").replace("-", "").replace(":", "")) + 4], fmt)
            except (ValueError, IndexError):
                continue
        return None
