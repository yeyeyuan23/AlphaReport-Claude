"""Recognize publication imprints without treating cited research as metadata."""

import re
from datetime import datetime


def is_cited_conference_prose(evidence: str) -> bool:
    """Identify affirmative citation/use prose, not an unrecognized venue label."""
    return bool(
        isinstance(evidence, str)
        and re.search(r"\b(?:conference|workshop|proceedings)\b", evidence, re.I)
        and re.search(
            r"\bet\s+al\b|\b(?:is|are|was|were)\s+"
            r"(?:(?:also|often|widely)\s+)?(?:used|cited|compared|adopted)\b|"
            r"\b(?:provides?|provided)\s+(?:our|the|a)\b",
            evidence,
            re.I,
        )
    )


def conference_edition_year(evidence: str) -> int | None:
    """Accept standalone venue imprints, including those below the abstract."""
    if not isinstance(evidence, str) or is_cited_conference_prose(evidence):
        return None
    line = evidence.strip()
    if not (
        re.match(r"[A-Z][A-Z0-9]+\s*[’']\d{2}(?:\s+workshop)?\s*,", line)
        or re.match(
            r"(?:proceedings\b|published as a (?:conference|workshop) paper\b)",
            line,
            re.I,
        )
        # A full venue heading may put its edition in either the title or a
        # badge. Keep the heading shape distinct from ordinary citation prose.
        or re.fullmatch(
            r"The (?:[A-Z0-9][A-Za-z0-9’'&-]* )*Conference "
            r"(?:on|of|for|in) [A-Za-z0-9 ’'&(),.-]+",
            line,
        )
        or re.fullmatch(r"\([A-Z][A-Z0-9]+[-’']\s*\d{2}\)", line)
    ):
        return None
    year = re.search(r"\b(?:19|20)\d{2}\b", line)
    short_year = re.search(r"\b[A-Z][A-Z0-9]+[ \t]*[-’']\s*(\d{2})\b", line)
    if year:
        return int(year.group(0))
    if short_year:
        return datetime.strptime(short_year[1], "%y").year
    return None
