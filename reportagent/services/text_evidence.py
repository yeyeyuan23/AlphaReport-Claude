"""Exact evidence matching with reversible PDF typesetting normalization.

No compatibility Unicode normalization, case folding, punctuation removal or fuzzy
matching: mathematical glyphs and substantive text remain distinct.
"""

from __future__ import annotations

import re
import unicodedata

LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}
CHUNK_CHARS = 4000


def text_chunks(text: str) -> dict[str, str]:
    return {
        f"C{i // CHUNK_CHARS + 1:03}": text[i : i + CHUNK_CHARS]
        for i in range(0, len(text), CHUNK_CHARS)
    }


def _normalized(text: str, *, unwrap: bool) -> tuple[str, list[tuple[int, int]]]:
    # Only alphabetic multi-letter fragments at a physical line boundary qualify.
    # In particular x-\ny and mathematical minus signs never become xy.
    deleted = set()
    if unwrap:
        for match in re.finditer(r"(?<=[a-z]{2})-[ \t]*\r?\n[ \t]*(?=[a-z]{2})", text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            context = text[line_start : line_end if line_end >= 0 else len(text)]
            if any(
                char in "=<>+*/^_\\$" or unicodedata.category(char) == "Sm"
                for char in context
            ):
                continue  # A line containing mathematical notation is ambiguous: fail closed.
            deleted.update(range(match.start(), match.end()))
    chars, offsets = [], []
    for index, char in enumerate(text):
        if index in deleted:
            continue
        if char.isspace():
            if chars and chars[-1] == " ":
                offsets[-1] = (offsets[-1][0], index + 1)
            else:
                chars.append(" ")
                offsets.append((index, index + 1))
            continue
        replacement = LIGATURES.get(char, char)
        chars.extend(replacement)
        offsets.extend([(index, index + 1)] * len(replacement))
    return "".join(chars), offsets


def locate_quote(
    text: str, quote: str, preferred_chunk: str | None = None
) -> dict | None:
    """Return raw character offsets and every chunk touched by an exact quote."""
    for unwrap in (False, True):
        source, mapping = _normalized(text, unwrap=unwrap)
        target, _ = _normalized(quote, unwrap=unwrap)
        target = target.strip()
        if not target:
            return None
        starts = []
        position = source.find(target)
        while position != -1:
            starts.append(position)
            position = source.find(target, position + 1)
        if not starts:
            continue

        def raw_start(position):
            return mapping[position][0]

        chosen = next(
            (
                position
                for position in starts
                if f"C{raw_start(position) // CHUNK_CHARS + 1:03}" == preferred_chunk
            ),
            starts[0],
        )
        start, end = mapping[chosen][0], mapping[chosen + len(target) - 1][1]
        ids = [
            f"C{i + 1:03}"
            for i in range(start // CHUNK_CHARS, (end - 1) // CHUNK_CHARS + 1)
        ]
        return {
            "chunk_id": ids[0],
            "chunk_ids": ids,
            "raw_start": start,
            "raw_end": end,
            "raw_quote": text[start:end],
            "normalization": "pdf_ligatures_linewrap_whitespace"
            if unwrap
            else "pdf_ligatures_whitespace",
            "locator": f"提取文本段 {'–'.join([ids[0], ids[-1]]) if len(ids) > 1 else ids[0]}；字符 [{start}, {end})",
        }
    return None
