"""Adapt report Markdown to the Markdown subset used by existing Feishu cards."""

from __future__ import annotations

import re


def card_markdown(text: str, *, omit_leading_title: bool = False) -> str:
    """Use bold section labels in cards; keep hashes in prose, links and code."""
    output = []
    fence = None
    seen_content = False
    for line in text.split("\n"):
        if fence is not None:
            output.append(line)
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + r"{" + str(fence[1]) + r",}[ \t]*",
                line,
            ):
                fence = None
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if opening:
            fence = (opening[1][0], len(opening[1]))
            seen_content = True
            output.append(line)
            continue
        # Cards do not render blockquote markers; keep their readable content.
        line = re.sub(r"^ {0,3}(?:>[ \t]+)+", "", line)
        heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*$", line)
        if heading:
            if omit_leading_title and not seen_content and len(heading[1]) == 1:
                seen_content = True
                continue  # The card already has its own title and date header.
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2])
            line = (
                title
                if title.startswith("**") and title.endswith("**")
                else f"**{title}**"
            )
        if line.strip():
            seen_content = True
        output.append(line)
    return "\n".join(output)


def card_sections(text: str, *, omit_leading_title: bool = False) -> list[dict]:
    """Use native section breaks instead of losing blank lines at arbitrary cuts.

    Empty lines at Markdown element edges do not supply reliable visual spacing
    in Feishu. Keep each major section together and separate it with native hr.
    """
    elements = []
    section = []
    fence = None

    def flush():
        content = card_markdown(
            "\n".join(section), omit_leading_title=omit_leading_title and not elements
        ).strip("\n")
        if content.strip():
            if elements:
                elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": content})
        section.clear()

    for line in text.split("\n"):
        if fence is not None:
            section.append(line)
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + r"{" + str(fence[1]) + r",}[ \t]*",
                line,
            ):
                fence = None
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if opening:
            fence = (opening[1][0], len(opening[1]))
        elif re.match(r"^ {0,3}##[ \t]+\S", line):
            flush()
        section.append(line)
    flush()
    return elements
