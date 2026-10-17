"""Stable document identity; title/author similarity alone is not provenance."""

import hashlib
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from reportagent.utils.hashing import content_hash


def normalized_doi(value):
    value = unquote(value or "").strip().lower()
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)


def normalized_url(value):
    if not value:
        return ""
    parts = urlsplit(value.strip())
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, "")
    )


def identities(value):
    source = getattr(value.source, "value", value.source)
    metadata = getattr(value, "raw_metadata", {})
    source_id = ""
    for key in ("info_code", "doc_path", "source_id"):
        if not metadata.get(key):
            continue
        source_id = str(metadata[key]).strip()
        if key == "doc_path" and source == "bigquant":
            # Search yields a relative path; hydration passes the same page's
            # absolute URL. Both identify one BigQuant document.
            parts = urlsplit(source_id)
            if not parts.netloc or parts.hostname in {
                "bigquant.com",
                "www.bigquant.com",
            }:
                source_id = urlunsplit(
                    ("", "", "/" + parts.path.lstrip("/"), parts.query, "")
                )
        break
    return {
        "doi": normalized_doi(value.doi),
        "arxiv": re.sub(r"v\d+$", "", (value.arxiv_id or "").strip()),
        "source_id": getattr(value, "source_item_id", None)
        or (f"{source}:{source_id}" if source_id else ""),
        "url": normalized_url(value.source_url),
    }


def identity_hash(value):
    keys = identities(value)
    for kind in ("doi", "arxiv", "source_id", "url"):
        if keys[kind]:
            token = [kind, keys[kind]]
            break
    else:
        token = [
            "unidentified",
            getattr(value.source, "value", value.source),
            content_hash(value.title, value.authors),
        ]
    return hashlib.sha256(json.dumps(token, ensure_ascii=False).encode()).hexdigest()


def compatible_match(incoming, existing):
    left, right = identities(incoming), identities(existing)
    # A conflicting DOI must never be bypassed through a shared page or title.
    if left["doi"] and right["doi"]:
        return left["doi"] == right["doi"]
    if left["arxiv"] and right["arxiv"]:
        return left["arxiv"] == right["arxiv"]
    if left["source_id"] and right["source_id"]:
        # IDs from different sources are independent namespaces. Within one
        # namespace a conflicting ID must never be bypassed via a shared URL.
        if left["source_id"].split(":", 1)[0] == right["source_id"].split(":", 1)[0]:
            return left["source_id"] == right["source_id"]
    # The previous identity implementation encoded source IDs only in the hash.
    # Recover those rows before considering a moved or renamed source URL.
    if left["source_id"] and existing.content_hash == identity_hash(incoming):
        return True
    if left["url"] and right["url"]:
        return left["url"] == right["url"]
    if any(left.values()) or any(right.values()):
        return False
    source = getattr(incoming.source, "value", incoming.source)
    try:
        authors = json.loads(existing.authors or "[]")
    except (TypeError, ValueError):
        return False
    return source == existing.source and content_hash(
        incoming.title, incoming.authors
    ) == content_hash(existing.title, authors)
