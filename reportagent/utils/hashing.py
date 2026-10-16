from __future__ import annotations

from typing import Optional

import hashlib
import re
import unicodedata


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def content_hash(title: str, authors  : Optional[list[str]] = None) -> str:
    norm_title = normalize_text(title)
    parts = [norm_title]
    if authors:
        sorted_authors = sorted(normalize_text(a) for a in authors)
        parts.extend(sorted_authors)
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
