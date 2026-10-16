"""Extract explicitly labelled sections; never rename a text prefix 'abstract'."""
from __future__ import annotations

import re

_PREFIX = r"^[ \t]*(?:\#{1,6}[ \t]+)?"
_NUMBER = r"(?:(?:\d+(?:\.\d+)*\.?|[IVX]+[.)])[ \t\n]+)?"
_SUFFIX = r"(?:[ \t]*[:：—–-][ \t]*|[ \t]*\.(?!\.)[ \t]*|[ \t]*(?=\n|$))"
_HEADINGS = {
    "abstract": re.compile(_PREFIX + r"(?:abstract|摘[ \t]*要)" + _SUFFIX, re.I | re.M),
    "introduction": re.compile(_PREFIX + _NUMBER + r"(?:introduction|引[ \t]*言|绪[ \t]*论)" + _SUFFIX, re.I | re.M),
}
_SECTION_NAMES = (
    r"abstract|introduction|keywords?|index terms|ccs concepts|acm reference format|"
    r"related work|background|methods?|methodology|experiments?|results|evaluation|"
    r"preliminaries|conclusions?|references|acknowledg(?:e)?ments|摘要|引言|绪论|关键词|参考文献"
)
_BOUNDARY = re.compile(
    _PREFIX + r"(?:"
    # Unknown numbered headings need a full line of heading-style capitalization,
    # not wrapped prose such as '500 stocks ...' or '2017 from Amazon ...'.
    r"(?:\d+(?:\.\d+)*\.?|[IVX]+[.)])[ \t]+"
    r"[A-Z][A-Za-z0-9-]*(?:[ \t]+(?:[A-Z][A-Za-z0-9-]*|and|or|of|for|in|with|the|a|an|to))*[ \t]*$"
    r"|(?:(?:\d+(?:\.\d+)*\.?|[IVX]+[.)])[ \t\n]+)?(?:" +
    # Only a complete heading or an explicit colon label ends a section.
    # 'methods usually adopt ...' is an ordinary sentence, not a heading.
    r"(?i:" + _SECTION_NAMES + r"))[ \t]*(?:[:：]|$))", re.M)
_ABSTRACT_FOOTER = re.compile(
    r"(?im)^[ \t]*(?:[∗*†‡][ \t]*(?:the|this|these|corresponding|equal|work)\b|"
    r"permission to make (?:digital|hard)|©|copyright\b)")


def extract_paper_sections(text: str) -> dict[str, str | None]:
    sections = {}
    for name, pattern in _HEADINGS.items():
        sections[name] = None
        for heading in pattern.finditer(text):
            line_end = text.find("\n", heading.start())
            line = text[heading.start():line_end if line_end >= 0 else len(text)]
            if re.search(r"\.{3,}", line):
                continue  # Table of contents, not section content.
            boundary = _BOUNDARY.search(text, heading.end())
            end = boundary.start() if boundary else len(text)
            if name == "abstract":
                footer = _ABSTRACT_FOOTER.search(text, heading.end(), end)
                if footer:
                    end = footer.start()
            value = text[heading.end():end].strip()
            if value:
                sections[name] = value
                break
    return sections
