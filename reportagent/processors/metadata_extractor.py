from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from reportagent.processors.pdf_extractor import PDFContent
from reportagent.utils.publication_dates import conference_edition_year


class MetadataExtractor:
    _DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
    _ARXIV_PATTERN = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?!\d)")
    _BARE_ARXIV = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?(?:\.pdf)?", re.I)
    _FRONTMATTER_END = re.compile(
        r"(?im)^[ \t]*(?:\#{1,6}[ \t]+)?(?:\d+(?:\.\d+)*\.?[ \t]+)?"
        r"(?:abstract|摘[ \t]*要|introduction|引言|references|参考文献)"
        r"(?:[ \t]*[:：.—–-][^\n]*|[ \t]*)$"
    )
    _REFERENCES_START = re.compile(
        r"(?im)^[ \t]*(?:\#{1,6}[ \t]+)?(?:\d+(?:\.\d+)*\.?[ \t]+)?"
        r"(?:references|参考文献)[ \t]*[:：]?[ \t]*$"
    )
    _ARXIV_REVISION_STAMP = re.compile(
        r"(?im)^[ \t]*arXiv:\s*(?P<id>\d{4}\.\d{4,5})v\d+"
        r"[ \t]+\[[^\]\n]+\][ \t]+(?P<date>\d{1,2}\s+[A-Za-z]+\s+\d{4})[ \t]*$"
    )

    def extract(self, pdf_content: PDFContent, *, fallback_text: str = "") -> dict:
        """Prefer native fields; fill gaps from compatible structured front matter."""
        meta = pdf_content.metadata
        first_page = pdf_content.first_page_text or (
            pdf_content.text if pdf_content.page_count <= 1 else ""
        )
        front = self._FRONTMATTER_END.split(first_page, maxsplit=1)[0]

        title = meta.get("title", "").strip()
        if not title:
            title = self._extract_title_from_text(front)

        authors = self._parse_authors(meta.get("author", ""))

        # Structured text can contain the entire paper. Cited identifiers in its
        # abstract/body/references must never become this paper's identity.
        doi_candidates = self._find_patterns(self._DOI_PATTERN, front)
        arxiv_candidates = self._find_patterns(self._ARXIV_PATTERN, front)
        # Native/OCR first-page extraction can put the rotated arXiv imprint
        # after the abstract. Only the standalone revision stamp is safe there.
        imprint = self._REFERENCES_START.split(first_page, maxsplit=1)[0]
        for stamp in self._ARXIV_REVISION_STAMP.finditer(imprint):
            if self._parse_date(stamp["date"]):
                arxiv_candidates.add(stamp["id"])

        date, date_source = self._publication_date(meta, first_page)
        date_info = self._publication_info(meta, first_page, date, date_source)

        material_type, material_info = self.extract_material_type(first_page)
        result = {
            "title": title,
            "authors": authors,
            "doi": next(iter(doi_candidates)) if len(doi_candidates) == 1 else None,
            "arxiv_id": next(iter(arxiv_candidates))
            if len(arxiv_candidates) == 1
            else None,
            "ambiguous_identity": len(doi_candidates) > 1 or len(arxiv_candidates) > 1,
            "date": date,
            "date_source": date_source,
            "pdf_creation_date": meta.get("creation_date"),
            "publication_date_info": date_info,
            "material_type": material_type,
            "material_type_info": material_info,
        }
        if fallback_text.strip() and fallback_text.strip() != first_page.strip():
            from reportagent.processors.pdf_extractor import PDFContent

            fallback = self.extract(PDFContent(text=fallback_text, page_count=1))
            result = self._merge_metadata(result, fallback)
        return result

    @staticmethod
    def _merge_metadata(native: dict, fallback: dict) -> dict:
        # An explicit identifier disagreement means the fallback cannot safely
        # supply *any* missing identity/date fields for this native paper.
        if (
            native["ambiguous_identity"]
            or fallback["ambiguous_identity"]
            or any(
                native[key]
                and fallback[key]
                and native[key].casefold() != fallback[key].casefold()
                for key in ("doi", "arxiv_id")
            )
        ):
            return native

        result = dict(native)
        for key in ("title", "doi", "arxiv_id"):
            if not result[key]:
                result[key] = fallback[key]

        if (
            result["material_type"] == "unknown"
            and fallback["material_type"] != "unknown"
        ):
            result["material_type"] = fallback["material_type"]
            result["material_type_info"] = fallback["material_type_info"]

        native_info = native["publication_date_info"]
        fallback_info = fallback["publication_date_info"]
        precision = {"unknown": 0, "year": 1, "month": 2, "day": 3}
        compatible = all(
            native_info[key] == fallback_info.get(key)
            for key in ("year", "month", "day")
            if key in native_info
        )
        if (
            native["date"] is None
            and compatible
            and (
                native_info["precision"] == "unknown"
                or precision[fallback_info["precision"]]
                > precision[native_info["precision"]]
            )
        ):
            result["date"] = fallback["date"]
            result["date_source"] = fallback["date_source"]
            # Retain separate native conference/revision evidence when refining
            # an unknown or partial publication date.
            result["publication_date_info"] = {
                **fallback_info,
                **{
                    key: value
                    for key, value in native_info.items()
                    if key
                    not in {"precision", "year", "month", "day", "source", "evidence"}
                },
            }
        return result

    def extract_material_type(self, first_page: str) -> tuple[str, dict]:
        # Header labels only: ordinary prose and references are not document type evidence.
        front = self._FRONTMATTER_END.split(first_page, maxsplit=1)[0]
        rules = (
            (
                "broker",
                r"^(?:[^\n|｜]{1,24}[|｜]\s*)?(?:证券研究报告|券商研究报告|证券公司研究报告|securities research report)(?:\s|$|[|｜:：·•—–-])",
            ),
            (
                "broker",
                r"^(?:本文|本篇|本报告)(?:来自|转载自|摘自)[^\n]{2,40}(?:证券|期货)(?:研究所|研究院)?[^\n]{0,60}(?:发布的报告|研究报告)",
            ),
            (
                "community",
                r"^(?:BigQuant|聚宽|JoinQuant)\s*(?:社区研究|community research)(?:\s|$|[|｜:：])",
            ),
            (
                "broker",
                r"^[\u4e00-\u9fff]{2,12}(?:证券|期货)(?:研究所|研究院)基于(?:策略|研究)?报告",
            ),
            (
                "academic",
                r"^(?:arXiv:\s*\d{4}\.\d{4,5}(?:v\d+)?\b|published as a (?:conference|workshop) paper\b|proceedings of\b)",
            ),
        )
        for kind, pattern in rules:
            for line in front.splitlines():
                line = re.sub(r"^\s*#{1,6}\s+", "", line).strip()
                if re.search(pattern, line, re.I):
                    return kind, {"source": "document_header", "evidence": line}
        if "分析师" in front and re.search(r"投资咨询号|执业资格|从业资格", front):
            for line in front.splitlines():
                line = line.strip()
                if re.fullmatch(
                    r"[\u4e00-\u9fff]{2,12}(?:证券|期货)(?:研究所|研究院)", line
                ):
                    return "broker", {
                        "source": "document_header",
                        "evidence": front.strip()[:800],
                    }
        # Rotated standalone arXiv stamps may be extracted below the abstract.
        imprint = self._REFERENCES_START.split(first_page, maxsplit=1)[0]
        stamp = self._ARXIV_REVISION_STAMP.search(imprint)
        if stamp and self._parse_date(stamp["date"]):
            return "academic", {
                "source": "document_imprint",
                "evidence": stamp.group(0).strip(),
            }
        return "unknown", {"source": "unknown"}

    def _extract_title_from_text(self, text: str) -> str:
        lines = []
        for line in text.splitlines():
            line = re.sub(r"^\s*#{1,6}\s+", "", line).strip()
            if not line or re.match(
                r"(?i)^(?:(?:digitized|scanned|downloaded|printed|generated)\s+"
                r"(?:by|from|with)\b|(?:arxiv|doi|published(?: online| on)?|"
                r"publication date|发表日期|出版日期|发布日期)\s*[:：]|"
                r"https?://|copyright\b|©|page\s+\d+\b|\d+\s*$)",
                line,
            ):
                continue
            if self._DOI_PATTERN.fullmatch(line) or self._BARE_ARXIV.fullmatch(line):
                continue
            lines.append(line)
        if lines:
            candidate = lines[0]
            if len(candidate) > 10:
                return candidate[:500]
            elif len(lines) > 1:
                return (candidate + " " + lines[1])[:500]
            return candidate
        return ""

    def _parse_authors(self, author_str: str) -> list[str]:
        if not author_str:
            return []
        for sep in [";", " and ", ","]:
            if sep in author_str:
                return [a.strip() for a in author_str.split(sep) if a.strip()]
        return [author_str.strip()] if author_str.strip() else []

    def _find_patterns(self, pattern: re.Pattern, text: str) -> set[str]:
        if pattern is self._DOI_PATTERN:
            prefix = r"(?i)^(?:doi(?=\s|:)|(?:https?://)?(?:dx\.)?doi\.org/)"
            bare = self._DOI_PATTERN
        else:
            prefix = r"(?i)^(?:arxiv(?=\s|:)|(?:https?://)?(?:www\.)?arxiv\.org/(?:abs|pdf)/)"
            bare = self._BARE_ARXIV
        # Even a first page may have unheaded prose citing another paper. Use
        # only explicit identifier lines, not every DOI/number in that prose.
        lines = [line.lstrip("#* \t").strip() for line in text.splitlines()]
        return {
            match.group(0).rstrip(".,;").casefold()
            for line in lines
            if re.match(prefix, line) or bare.fullmatch(line.rstrip(".,;"))
            for match in pattern.finditer(line)
        }

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
            for value, partial_source, evidence in [
                (meta.get(k, ""), f"metadata:{k}", meta.get(k, ""))
                for k in ("publication_date", "published_date")
            ] + [
                (m["date"], "labelled_publication_date", m.group(0).strip())
                for m in labels
            ]:
                partial = self._partial_date(value)
                if partial:
                    info = {
                        **partial,
                        "source": partial_source,
                        "evidence": evidence,
                    }
                    break
        imprint = self._REFERENCES_START.split(first_page, maxsplit=1)[0]
        # A conference's edition establishes the year, never its publication day.
        conference = None
        for line in imprint.splitlines():
            year = conference_edition_year(line)
            if year is not None:
                conference = {
                    "year": year,
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
