"""Shared reply presentation and complete, bounded delivery for bot/report paths."""

from __future__ import annotations

import json
import re
import uuid

from reportagent.services.feishu_markdown import card_sections

from reportagent.services.feishu_transport import (
    CHUNK_BYTES,
    text_chunks as _chunks,
    send_payload,
)


def _cells(line: str) -> list[str]:
    """Split table pipes outside escaped text and inline code."""
    cells, current, ticks, escaped = [], [], False, False
    line = line.strip().strip("|")
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == "`":
            ticks = not ticks
            current.append(char)
        elif char == "|" and not ticks:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def readable_tables(text: str) -> str:
    """Render complete simple GFM tables as labelled rows for narrow cards."""
    lines, output, index, fence = text.split("\n"), [], 0, None
    while index < len(lines):
        line = lines[index]
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif (
                marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not line[marker.end() :].strip()
            ):
                fence = None
        if fence is None and "|" in line and index + 1 < len(lines):
            headers, separators = _cells(line), _cells(lines[index + 1])
            if (
                len(headers) > 1
                and len(headers) == len(separators)
                and all(
                    re.fullmatch(r":?-{3,}:?", s.replace(" ", "")) for s in separators
                )
            ):
                end, rows = index + 2, []
                while end < len(lines) and "|" in lines[end] and lines[end].strip():
                    cells = _cells(lines[end])
                    if len(cells) != len(headers):
                        break
                    rows.append(cells)
                    end += 1
                if rows:
                    output.append(
                        "\n\n".join(
                            "- "
                            + "；".join(f"**{h}**：{v}" for h, v in zip(headers, row))
                            for row in rows
                        )
                    )
                    index = end
                    continue
        output.append(line)
        index += 1
    return "\n".join(output)


def _inline_parts(line: str) -> list[str]:
    """Keep navigation and short inline formatting atomic across card boundaries."""
    spans = []
    for match in re.finditer(r"!?\[[^\]\n]*\]\(", line):
        end, depth, escaped = match.end(), 1, False
        while end < len(line) and line[end] != "\n" and depth:
            char = line[end]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            end += 1
        if depth == 0:
            spans.append((match.start(), end))
    spans.extend(
        (m.start(), m.end())
        for m in re.finditer(r"https?://[^\s<>]+|\*\*[^*\n]+\*\*|`[^`\n]+`", line)
    )
    merged = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    parts, cursor = [], 0
    for start, end in merged:
        parts.extend(_chunks(line[cursor:start]))
        parts.append(line[start:end])
        cursor = end
    parts.extend(_chunks(line[cursor:]))
    return parts


def _markdown_chunks(text: str) -> list[str]:
    parts, current, fence, size = [], [], None, 0
    for line in text.splitlines(keepends=True):
        for piece in _chunks(line) if fence else _inline_parts(line):
            length = len(piece.encode("utf-8"))
            if size + length > CHUNK_BYTES and current:
                parts.append("".join(current) + ("\n" + fence[0] if fence else ""))
                current = [fence[1] + "\n"] if fence else []
                size = sum(len(s.encode("utf-8")) for s in current)
            current.append(piece)
            size += length
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", piece.rstrip("\n"))
            if marker:
                if fence is None:
                    fence = (marker[1], piece.rstrip("\n"))
                elif (
                    marker[1][0] == fence[0][0]
                    and len(marker[1]) >= len(fence[0])
                    and not marker[2].strip()
                ):
                    fence = None
    if current:
        parts.append("".join(current) + ("\n" + fence[0] if fence else ""))
    return parts


def response_payloads(text: str, *, title="AlphaReport 回复") -> list[tuple[str, str]]:
    if not text:
        return []
    rich = bool(
        re.search(r"(?m)^\s*#{1,6}\s|\*\*|```|~~~|\[[^\]\n]+\]\(https?://|^\s*\|", text)
    )
    if not rich:
        return [
            ("text", json.dumps({"text": part}, ensure_ascii=False))
            for part in _chunks(text)
        ]
    rendered = readable_tables(text)
    rendered = re.sub(r"(?m)^【([^\n]+)】\s*$", r"## \1", rendered)
    parts = _markdown_chunks(rendered)
    result = []
    for index, part in enumerate(parts, 1):
        label = title + (f" · {index}/{len(parts)}" if len(parts) > 1 else "")
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": label},
                "template": "wathet",
            },
            "elements": card_sections(part),
        }
        result.append(("interactive", json.dumps(card, ensure_ascii=False)))
    return result


class DeliveryCancelled(RuntimeError):
    """The owning conversation was reset before an unsent response part."""


def deliver_response(
    client,
    text: str,
    receive_id: str,
    *,
    receive_type="chat_id",
    reply_to=None,
    request_id=None,
    should_send=None,
    title="AlphaReport 回复",
) -> list[str]:
    """Deliver all parts, with complete plain-text recovery after a card failure."""

    def send(parts, *, fallback=False):
        ids = []
        for index, (kind, content) in enumerate(parts):
            if should_send is not None and not should_send():
                raise DeliveryCancelled("Conversation reset before delivery")
            part_id = None
            if request_id:
                part_id = (
                    request_id
                    if index == 0 and not fallback
                    else str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            request_id
                            + (":fallback:" if fallback else ":")
                            + str(index),
                        )
                    )
                )
            ids.append(
                send_payload(
                    client,
                    kind,
                    content,
                    receive_id,
                    receive_type=receive_type,
                    reply_to=reply_to,
                    request_id=part_id,
                )
            )
        return ids

    parts = response_payloads(text, title=title)
    try:
        return send(parts)
    except DeliveryCancelled:
        raise
    except Exception:
        if not any(kind == "interactive" for kind, _ in parts):
            raise
        return send(
            [
                ("text", json.dumps({"text": part}, ensure_ascii=False))
                for part in _chunks(text)
            ],
            fallback=True,
        )
