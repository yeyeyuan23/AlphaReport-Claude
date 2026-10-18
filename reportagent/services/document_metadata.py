"""Content metadata, independent of the transport used to import a document."""

from __future__ import annotations

import json
import re
from datetime import datetime

from reportagent.utils.publication_dates import is_cited_conference_prose

METADATA_VERSION = 2
MATERIAL_TYPES = {"academic", "broker", "community", "other", "unknown"}


def infer_material_type(paper: dict) -> tuple[str, dict]:
    """Use document evidence or explicit metadata, never its distribution channel."""
    from reportagent.processors.metadata_extractor import MetadataExtractor

    raw = paper.get("raw_metadata") or {}
    value = raw.get("material_type")
    info = metadata_dict(raw.get("material_type_info"))
    if value in MATERIAL_TYPES - {"unknown"} and info.get("source") != "typed_feed":
        return value, info or {"source": "explicit_metadata", "evidence": value}
    extractor = MetadataExtractor()
    for text in (paper.get("full_text"), paper.get("abstract")):
        if text:
            value, info = extractor.extract_material_type(text[:8000])
            if value != "unknown":
                return value, info
    arxiv_id = paper.get("arxiv_id") or ""
    if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?", arxiv_id):
        return "academic", {
            "source": "document_identifier",
            "evidence": f"arXiv:{arxiv_id}",
        }
    title = paper.get("title") or ""
    if re.match(
        r"^[【\[]?[\u4e00-\u9fff]{2,12}(?:证券|期货)[】\]]?\s*[-—–:：|｜]", title
    ) and re.search(
        r"(?:研究报告|专题报告|深度报告|策略报告|系列(?:研究|专题|之))", title
    ):
        return "broker", {"source": "document_title", "evidence": title}
    return "unknown", {"source": "unknown"}


def metadata_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def material_type(paper: dict) -> str:
    value = paper.get("material_type")
    return value if value in MATERIAL_TYPES else "unknown"


def has_publication_date(paper) -> bool:
    """Partial dates are known dates; never invent their missing month/day."""
    get = (
        paper.get if isinstance(paper, dict) else lambda key: getattr(paper, key, None)
    )
    info = metadata_dict(
        get("publication_date_info") or get("publication_date_info_json")
    )
    if info.get("source") in {
        "pdf_creation_date",
        "creation_date",
        "import_date",
        "created_at",
    }:
        return False
    if get("published_date"):
        return True
    precision = info.get("precision")
    if (
        precision not in {"year", "month", "day"}
        or not info.get("evidence")
        or info.get("source") in {None, "", "unknown"}
    ):
        return False
    try:
        year = info["year"]
        month = info["month"] if precision in {"month", "day"} else 1
        day = info["day"] if precision == "day" else 1
        if any(type(v) is not int for v in (year, month, day)):
            return False
        datetime(year, month, day)
        return True
    except (KeyError, TypeError, ValueError):
        return False


def has_precise_publication_date(paper) -> bool:
    """A daily publication needs a real day, not a guessed partial/file date."""
    get = (
        paper.get if isinstance(paper, dict) else lambda key: getattr(paper, key, None)
    )
    info = metadata_dict(
        get("publication_date_info") or get("publication_date_info_json")
    )
    return (
        bool(get("published_date"))
        and info.get("precision") in {None, "day"}
        and has_publication_date(paper)
    )


def merge_document_metadata(row, search_result) -> None:
    """Fill metadata without erasing supported values on inconclusive reparses."""
    raw = search_result.raw_metadata
    from reportagent.sources.date_window import source_date

    # Online sources own their publication date. Accept evidenced corrections in
    # either direction; a missing date during PDF hydration cannot erase it.
    new_info = metadata_dict(raw.get("publication_date_info"))
    incoming = source_date(search_result.published_date)
    if (
        search_result.source.value in {"arxiv", "eastmoney", "bigquant"}
        and incoming is not None
    ):
        if row.published_date is None or (
            row.source == search_result.source.value and new_info.get("source")
        ):
            row.published_date = incoming
            if new_info:
                row.publication_date_info_json = json.dumps(
                    new_info, ensure_ascii=False
                )
    old = metadata_dict(row.publication_date_info_json)
    new = metadata_dict(raw.get("publication_date_info"))
    legacy = old.get("source") in {
        "pdf_creation_date",
        "creation_date",
        "import_date",
        "created_at",
    }
    if legacy:
        row.published_date = None
        old = {}
        row.publication_date_info_json = None
    incoming_date = source_date(search_result.published_date)
    # Repair a positively identified citation-year inference only when
    # the same record receives explicit, evidenced publication metadata.
    # Missing metadata or a conflicting *supported* date must remain stable.
    if (
        row.published_date is None
        and old.get("source") == "conference_edition"
        and old.get("precision") == "year"
        and is_cited_conference_prose(old.get("evidence"))
        and new.get("source")
        in {
            "labelled_publication_date",
            "metadata:publication_date",
            "metadata:published_date",
        }
        and has_publication_date({"publication_date_info": new})
        and (
            incoming_date is None
            or all(
                new[key] == getattr(incoming_date, key)
                for key in ("year", "month", "day")
                if key in new
            )
        )
    ):
        old = {}
    compatible_date = incoming_date is not None and all(
        old[key] == getattr(incoming_date, key)
        for key in ("year", "month", "day")
        if key in old
    )
    if row.published_date is None and incoming_date is not None and compatible_date:
        row.published_date = incoming_date
        if new:
            row.publication_date_info_json = json.dumps(new, ensure_ascii=False)
    elif row.published_date is None:
        precision = {"unknown": 0, "year": 1, "month": 2, "day": 3}
        compatible = all(
            old[key] == new.get(key) for key in ("year", "month", "day") if key in old
        )
        if not old or (
            compatible
            and precision.get(new.get("precision"), 0)
            > precision.get(old.get("precision"), 0)
        ):
            row.publication_date_info_json = (
                json.dumps(new, ensure_ascii=False) if new else None
            )
    value, type_info = infer_material_type(
        {
            "title": search_result.title,
            "full_text": search_result.full_text,
            "abstract": search_result.abstract,
            "arxiv_id": search_result.arxiv_id,
            "raw_metadata": raw,
        }
    )
    old_type_info = metadata_dict(row.material_type_info_json)
    legacy_type = old_type_info.get("source") == "typed_feed"
    if (
        value in MATERIAL_TYPES
        and value != "unknown"
        and (row.material_type in {None, "", "unknown"} or legacy_type)
    ):
        row.material_type = value
        row.material_type_info_json = json.dumps(type_info, ensure_ascii=False)
    elif not row.material_type or legacy_type:
        row.material_type = "unknown"
        row.material_type_info_json = json.dumps({"source": "unknown"})
