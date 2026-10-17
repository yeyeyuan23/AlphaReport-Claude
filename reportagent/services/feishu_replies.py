"""Shared reply presentation and complete, bounded delivery for bot/report paths."""

from __future__ import annotations

import json
import re
import uuid

from reportagent.services.feishu_markdown import card_sections

CHUNK_BYTES = 7000  # Conservative content budget, including CJK and emoji.


def _chunks(text: str, limit: int = CHUNK_BYTES) -> list[str]:
    chunks, current, size = [], [], 0
    for char in text:
        length = len(char.encode("utf-8"))
        if size + length > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += length
    if current:
        chunks.append("".join(current))
    return chunks


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


def deliver_response(
    client,
    text: str,
    receive_id: str,
    *,
    receive_type="chat_id",
    reply_to=None,
    request_id=None,
    title="AlphaReport 回复",
) -> list[str]:
    """Stop on a failed part; never report partial content as complete delivery."""
    from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
    from lark_oapi.api.im.v1.model.create_message_request_body import (
        CreateMessageRequestBody,
    )
    from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
    from lark_oapi.api.im.v1.model.reply_message_request_body import (
        ReplyMessageRequestBody,
    )

    ids = []
    for index, (kind, content) in enumerate(response_payloads(text, title=title)):
        if reply_to:
            body = ReplyMessageRequestBody.builder().msg_type(kind).content(content)
            request = (
                ReplyMessageRequest.builder()
                .message_id(reply_to)
                .request_body(body.build())
                .build()
            )
            response = client.im.v1.message.reply(request)
        else:
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(kind)
                .content(content)
            )
            if request_id:
                body.uuid(
                    request_id
                    if index == 0
                    else str(
                        uuid.uuid5(uuid.NAMESPACE_URL, request_id + ":" + str(index))
                    )
                )
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_type)
                .request_body(body.build())
                .build()
            )
            response = client.im.v1.message.create(request)
        if not response.success() or not response.data or not response.data.message_id:
            raise RuntimeError(
                f"Feishu response part {index + 1} failed: code={getattr(response, 'code', 'unknown')}"
            )
        ids.append(response.data.message_id)
    return ids
