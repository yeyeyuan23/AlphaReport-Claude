"""Input identity and verified evidence checks for automatic classification refreshes."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

from reportagent.models.schemas import AssetClass, Frequency, Market, Topic

_DIMENSIONS = {
    "markets": ("market", Market),
    "asset_classes": ("asset_class", AssetClass),
    "frequencies": ("frequency", Frequency),
    "topics": ("topic", Topic),
}
_OWNED_TAGS = {dimension for dimension, _ in _DIMENSIONS.values()} | {"custom"}


def classification_material(paper) -> str:
    """Resolve the same bounded evidence for source results and persisted reports."""
    abstract = paper.abstract or ""
    if abstract.strip():
        return abstract
    metadata = getattr(paper, "raw_metadata", None) or {}
    introduction = metadata.get("introduction")
    if not introduction:
        from reportagent.processors.paper_sections import extract_paper_sections

        introduction = extract_paper_sections(paper.full_text or "")["introduction"]
    return f"【原文引言；未提取到论文摘要】\n{introduction}" if introduction else ""


def input_fingerprint(title: str, material: str) -> str:
    return hashlib.sha256((title + "\n" + material).encode()).hexdigest()


def failed_attempt(title: str, material: str, error_kind: str) -> dict:
    """Keep only one safe attempt summary, never the upstream exception text."""
    return {
        "schema_version": 1,
        "status": "failed",
        "input_sha256": input_fingerprint(title, material),
        "error_kind": error_kind,
        "attempted_at": datetime.now(timezone.utc).isoformat(),
    }


def reusable_provenance(row, search_result, classification) -> dict | None:
    """Only a failed automatic refresh of the same proven input may retain labels.

    Unknown/legacy evidence is not proof. Recheck the persisted label/tag set and
    exact evidence spans rather than trusting a fingerprint on an arbitrary dict.
    Explicit rule reclassification has no failed-attempt marker and bypasses this.
    """
    title = search_result.title or ""
    # Stored abstract can belong to an earlier source; the accepted fingerprint
    # identifies the actual classifier input, including a labelled introduction.
    material = classification_material(search_result)
    fingerprint = input_fingerprint(title, material)
    attempt = classification.provenance.get("last_failed_attempt")
    if (
        classification.method not in {"unverified", "rule"}
        or not isinstance(attempt, dict)
        or type(attempt.get("schema_version")) is not int
        or attempt["schema_version"] != 1
        or attempt.get("status") != "failed"
        or attempt.get("error_kind") not in {"invalid_evidence", "model_error"}
        or attempt.get("input_sha256") != fingerprint
        or row.classification_method not in {"llm", "hybrid"}
        or row.title != title
    ):
        return None
    try:
        provenance = json.loads(row.classification_provenance_json or "null")
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(provenance, dict)
        or type(provenance.get("schema_version")) is not int
        or provenance["schema_version"] != 1
        or "status" in provenance
        or provenance.get("input_sha256") != fingerprint
        or not isinstance(provenance.get("labels"), dict)
        or not provenance["labels"]
        or not isinstance(provenance.get("rejected_labels"), list)
        or any(not isinstance(key, str) for key in provenance["rejected_labels"])
        or type(row.classification_confidence) not in (int, float)
        or not math.isfinite(row.classification_confidence)
        or not 0 <= row.classification_confidence <= 1
    ):
        return None

    expected, tag_pairs = set(), set()
    for field, (dimension, enum_cls) in _DIMENSIONS.items():
        stored = getattr(row, field)
        if not isinstance(stored, str):
            return None
        values = stored.split(",") if stored else []
        if len(values) != len(set(values)) or not set(values) <= {
            item.value for item in enum_cls
        }:
            return None
        expected.update(f"{field}.{value}" for value in values)
        tag_pairs.update((dimension, value) for value in values)
    for tag in row.tags:
        if tag.dimension == "custom":
            if not isinstance(tag.value, str) or not tag.value.strip():
                return None
            expected.add(f"custom_tags.{tag.value}")
            tag_pairs.add(("custom", tag.value))
    if set(provenance["labels"]) != expected or tag_pairs != {
        (tag.dimension, tag.value) for tag in row.tags if tag.dimension in _OWNED_TAGS
    }:
        return None
    for evidence in provenance["labels"].values():
        if not isinstance(evidence, dict):
            return None
        source = evidence.get("source")
        quote, reason = evidence.get("quote"), evidence.get("reason")
        start, end = evidence.get("raw_start"), evidence.get("raw_end")
        if (
            source not in ("title", "material")
            or not isinstance(quote, str)
            or sum(char.isalnum() for char in quote) < 2
            or not isinstance(reason, str)
            or not reason.strip()
            or type(start) is not int
            or type(end) is not int
        ):
            return None
        text = title if source == "title" else material
        if not 0 <= start < end <= len(text) or text[start:end] != quote:
            return None
    return {**provenance, "last_failed_attempt": attempt}
