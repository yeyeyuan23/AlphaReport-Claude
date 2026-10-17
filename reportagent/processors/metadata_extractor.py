from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from reportagent.processors.pdf_extractor import PDFContent


class MetadataExtractor:
    _DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
    _ARXIV_PATTERN = re.compile(r"(\d{4}\.\d{4,5})")
    _FRONTMATTER_END = re.compile(
        r"(?im)^[ \t]*(?:\#{1,6}[ \t]+)?(?:\d+(?:\.\d+)*\.?[ \t]+)?"
        r"(?:abstract|摘[ \t]*要|references|参考文献)(?:[ \t]*[:：.—–-][^\n]*|[ \t]*)$"
    )
    _REFERENCES_START = re.compile(
        r"(?im)^[ \t]*(?:\#{1,6}[ \t]+)?(?:\d+(?:\.\d+)*\.?[ \t]+)?"
        r"(?:references|参考文献)[ \t]*[:：]?[ \t]*$"
    )

    def extract(self, pdf_content: PDFContent) -> dict:
        meta = pdf_content.metadata
        first_page = getattr(pdf_content, "first_page_text", "") or pdf_content.text

        title = meta.get("title", "").strip()
        if not title:
            title = self._extract_title_from_text(first_page)

        authors = self._parse_authors(meta.get("author", ""))

        doi = self._find_pattern(self._DOI_PATTERN, first_page)
        arxiv_id = self._find_pattern(self._ARXIV_PATTERN, first_page)

        date, date_source = self._publication_date(meta, first_page)
        date_info = self._publication_info(meta, first_page, date, date_source)

        return {
            "title": title,
            "authors": authors,
            "doi": doi,
            "arxiv_id": arxiv_id,
            "date": date,
            "date_source": date_source,
            "pdf_creation_date": meta.get("creation_date"),
            "publication_date_info": date_info,
        }

    def _extract_title_from_text(self, text: str) -> str:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
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

    def _publication_date(self, meta: dict, first_page: str):
        for key in ("publication_date", "published_date"):
            date = self._parse_date(meta.get(key, ""))
            if date is not None:
                return date, f"metadata:{key}"
        # Inspect labelled front matter only, excluding cited publication dates.
        front = self._FRONTMATTER_END.split(first_page, maxsplit=1)[0]
        for match in re.finditer(
            r"(?im)^[ \t]*(?:published(?: online| on)?|publication date|发表日期|出版日期|发布日期)"
            r"[ \t]*[:：][ \t]*(?P<date>[^\n]+)",
            front,
        ):
            date = self._parse_date(match["date"])
            if date is not None:
                return date, "labelled_publication_date"
        return None, "unknown"

    def _publication_info(self, meta, first_page, date, source):
        """Preserve evidence/precision; conference and revision dates are not publication days."""
        info = {"precision": "unknown", "source": "unknown"}
        if date is not None:
            info = {
                "precision": "day",
                "year": date.year,
                "month": date.month,
                "day": date.day,
                "source": source,
            }
        front = self._FRONTMATTER_END.split(first_page, maxsplit=1)[0]
        labels = list(
            re.finditer(
                r"(?im)^[ \t]*(?:published(?: online| on)?|publication date|发表日期|出版日期|发布日期)"
                r"[ \t]*[:：][ \t]*(?P<date>[^\n]+)",
                front,
            )
        )
        evidence = (
            next(
                (
                    m.group(0).strip()
                    for m in labels
                    if self._parse_date(m["date"]) == date
                ),
                None,
            )
            if date
            else None
        )
        if date:
            info["evidence"] = evidence or str(
                meta.get(source.removeprefix("metadata:"), "")
            )
        else:
            for value, label in [
                (meta.get(k, ""), f"metadata:{k}")
                for k in ("publication_date", "published_date")
            ] + [(m["date"], m.group(0).strip()) for m in labels]:
                partial = self._partial_date(value)
                if partial:
                    info = {
                        **partial,
                        "source": "labelled_publication_date",
                        "evidence": label,
                    }
                    break
        imprint = self._REFERENCES_START.split(first_page, maxsplit=1)[0]
        # A conference's edition establishes the year, never its publication day.
        conference = None
        for line in imprint.splitlines():
            # Generic venue imprints/badges, not ordinary prose citing prior work.
            if not (
                re.match(r"\s*[A-Z][A-Z0-9]+\s*[’']\d{2}(?:\s+workshop)?\s*,", line)
                or re.search(
                    r"^(?:proceedings\b|the\b.*conference\b|published as a (?:conference|workshop) paper\b)",
                    line.strip(),
                    re.I,
                )
                or re.fullmatch(r"\s*\([A-Z][A-Z0-9]+[-’']\s*\d{2}\)\s*", line)
            ):
                continue
            year = re.search(r"\b(?:19|20)\d{2}\b", line)
            short_year = re.search(r"\b[A-Z][A-Z0-9]+[ \t]*[-’']\s*(\d{2})\b", line)
            if year or short_year:
                conference = {
                    "year": int(year.group(0))
                    if year
                    else datetime.strptime(short_year[1], "%y").year,
                    "evidence": line.strip(),
                }
                break
        copyright = re.search(
            r"(?im)^[ \t]*(?:copyright[ \t]*©|©)[ \t]*(?P<year>\d{4})[^\n]*", imprint
        )
        if info["precision"] == "unknown":
            if conference:
                info = {
                    "precision": "year",
                    "year": conference["year"],
                    "source": "conference_edition",
                    "evidence": conference["evidence"],
                }
            elif copyright:
                info = {
                    "precision": "year",
                    "year": int(copyright["year"]),
                    "source": "copyright_year",
                    "evidence": copyright.group(0).strip(),
                }
        if conference:
            # Keep the full imprint as separate context, including any event range.
            info["conference_evidence"] = conference["evidence"]
        revision = re.search(
            r"arXiv:\s*\d{4}\.\d{4,5}(?P<version>v\d+)[^\n]*?"
            r"(?P<date>\d{1,2}\s+[A-Za-z]+\s+\d{4})",
            imprint,
            re.I,
        )
        if revision:
            stamp = self._parse_date(revision["date"])
            if stamp:
                info.update(
                    arxiv_version=revision["version"],
                    arxiv_version_date=stamp.date().isoformat(),
                    arxiv_version_evidence=revision.group(0),
                )
        return info

    def _partial_date(self, value):
        if not isinstance(value, str) or not value.strip():
            return None
        for fmt in ("%Y-%m", "%Y/%m", "%B %Y", "%b %Y", "%Y年%m月"):
            try:
                stamp = datetime.strptime(value.strip(), fmt)
                return {"precision": "month", "year": stamp.year, "month": stamp.month}
            except ValueError:
                pass
        if re.fullmatch(r"\d{4}年?", value.strip()):
            return {"precision": "year", "year": int(value.strip().rstrip("年"))}
        return None

    def _parse_date(self, date_str: str) -> Optional[datetime]:
        if not date_str:
            return None
        date_str = date_str.strip()
        for fmt in (
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y年%m月%d日",
            "%d %B %Y",
            "%d %b %Y",
            "%B %d, %Y",
        ):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return None
