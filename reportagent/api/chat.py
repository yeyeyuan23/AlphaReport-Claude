from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from reportagent.chat.agent import build_chat_agent
from reportagent.api.deps import get_or_create_user
from reportagent.utils.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


class Conversation:
    def __init__(self, conversation_id: str, user_id: str = "", email: str = ""):
        self.id = conversation_id
        self.user_id = user_id
        self.email = email
        self.title = ""
        self.messages: list[dict[str, str]] = []
        self.referenced_reports: dict[
            int, dict
        ] = {}  # report_id → {title, source, topics}
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.created_at


_conversations: dict[str, Conversation] = {}


def _extract_report_ids(text: str) -> list[int]:
    """Extract report IDs from text patterns like [123] or [ID: 456]."""
    import re

    ids = []
    for m in re.finditer(r"\[(\d{1,6})\]", text):
        rid = int(m.group(1))
        if rid > 0 and rid not in ids:
            ids.append(rid)
    return ids


def _update_report_cache(conv: Conversation, text: str) -> None:
    """Scan response text for report references and cache their metadata from DB."""
    ids = _extract_report_ids(text)
    if not ids:
        return

    new_ids = [rid for rid in ids if rid not in conv.referenced_reports]
    if not new_ids:
        return

    try:
        from reportagent.db.engine import get_session_factory
        from reportagent.db.repository import ReportRepository

        factory = get_session_factory()
        session = factory()
        try:
            repo = ReportRepository(session)
            for rid in new_ids[:20]:  # cap per turn
                if rid in conv.referenced_reports:
                    continue
                r = repo.get_report(rid)
                if r:
                    conv.referenced_reports[rid] = {
                        "title": r.title,
                        "source": r.source or "",
                        "topics": r.topics or "",
                        "date": r.published_date.strftime("%Y-%m-%d")
                        if r.published_date
                        else "",
                    }
        finally:
            session.close()
    except Exception as e:
        logger.debug("Failed to update report cache: %s", e)


def _build_report_context(conv: Conversation) -> str:
    """Build a context summary of all reports discussed in this conversation."""
    if not conv.referenced_reports:
        return ""

    lines = ["## 本对话中已讨论的研报（可直接引用，无需重新搜索）"]
    for rid, info in conv.referenced_reports.items():
        date = info.get("date", "")
        lines.append(
            f"- [{rid}] {info['title']} (来源: {info['source']}, 主题: {info['topics']}{', 日期: ' + date if date else ''})"
        )
    return "\n".join(lines)


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(..., min_length=1)
    mode: str | None = None  # "task" (default) or "brainstorm"


def _sse(event_type: str, data: Any) -> str:
    payload = json.dumps(
        {"type": event_type, **(data if isinstance(data, dict) else {"content": data})},
        ensure_ascii=False,
    )
    return f"data: {payload}\n\n"


async def _stream_response(
    conversation_id: str,
    user_message: str,
    mode: str | None = None,
    user_id: str = "",
    email: str = "",
):
    conv = _conversations.get(conversation_id)
    if not conv:
        conv = Conversation(conversation_id, user_id=user_id, email=email)
        _conversations[conversation_id] = conv
    elif user_id and not conv.user_id:
        conv.user_id = user_id
        conv.email = email or conv.email

    if not conv.title:
        conv.title = user_message[:50]

    conv.messages.append({"role": "user", "content": user_message})
    conv.updated_at = datetime.now(timezone.utc).isoformat()

    # Build LangChain messages from recent history
    lc_messages = []
    max_hist = get_config("chat", "max_history_messages", default=50)
    recent = conv.messages[-max_hist:]
    for m in recent:
        if m["role"] == "user":
            lc_messages.append(HumanMessage(content=m["content"]))
        elif m["role"] == "assistant":
            lc_messages.append(AIMessage(content=m["content"]))

    # Inject referenced reports context so the agent remembers what was discussed
    report_ctx = _build_report_context(conv)
    if report_ctx:
        lc_messages.insert(0, SystemMessage(content=report_ctx))

    # Inject authenticated user context. In production this should come from
    # session/JWT middleware, not from client-provided fields.
    if user_id or email:
        user_lines = [
            "## 当前用户上下文",
            f"- user_id: {user_id or 'unknown'}",
            f"- email: {email or 'unknown'}",
        ]
        lc_messages.insert(0, SystemMessage(content="\n".join(user_lines)))

    # Inject mode hint for brainstorm mode
    if mode == "brainstorm":
        lc_messages.insert(
            0,
            SystemMessage(
                content="[用户已选择创意伙伴模式] 请以研究协作者身份对话：主动使用 brainstorm_research 和 semantic_search_reports 工具，"
                "帮助用户从已有文献中发现研究空白、提出新思路。多问引导性问题，不要只是罗列论文。"
            ),
        )

    try:
        yield _sse("status", {"content": "thinking"})

        agent_version = get_config("chat", "agent_version", default="v2")
        logger.debug("Using agent version: %s", agent_version)

        if agent_version == "v2":
            full_response = ""
            async for evt in _stream_v2(lc_messages):
                yield _sse(evt["type"], evt)
                if evt.get("type") == "token":
                    full_response += evt.get("content", "")
        else:
            agent = build_chat_agent()
            full_response = ""

            async for event in agent.astream_events(
                {"messages": lc_messages},
                version="v2",
            ):
                kind = event.get("event", "")
                data = event.get("data", {})

                if kind == "on_chat_model_stream":
                    chunk = data.get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        content = chunk.content
                        if isinstance(content, str) and content:
                            full_response += content
                            yield _sse("token", content)
                    if chunk and hasattr(chunk, "additional_kwargs"):
                        reasoning = chunk.additional_kwargs.get("reasoning_content", "")
                        if reasoning:
                            yield _sse("thinking", reasoning)

                elif kind == "on_tool_start":
                    tool_name = event.get("name", "")
                    tool_input = data.get("input", {})
                    yield _sse(
                        "tool_call",
                        {
                            "name": tool_name,
                            "args": tool_input
                            if isinstance(tool_input, dict)
                            else {"input": str(tool_input)},
                        },
                    )

                elif kind == "on_tool_end":
                    tool_name = event.get("name", "")
                    output = data.get("output", "")
                    output_str = (
                        str(output.content)
                        if hasattr(output, "content")
                        else str(output)
                    )
                    yield _sse(
                        "tool_result",
                        {
                            "name": tool_name,
                            "result": output_str[:2000],
                        },
                    )

        if full_response:
            conv.messages.append({"role": "assistant", "content": full_response})
            # Cache referenced reports for future turns
            _update_report_cache(conv, full_response)

        yield _sse("done", {"conversation_id": conversation_id})

    except Exception as e:
        logger.exception("Chat stream error")
        yield _sse("error", str(e))


async def _stream_v2(lc_messages: list):
    """Stream events from the Plan-Execute-Reflect agent."""
    from reportagent.chat.graph import run_with_stream

    async for evt in run_with_stream(lc_messages):
        yield evt


@router.post("/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_or_create_user)):
    conversation_id = req.conversation_id or str(uuid.uuid4())
    return StreamingResponse(
        _stream_response(
            conversation_id,
            req.message,
            req.mode,
            user_id=str(user["id"]) if user["id"] else "",
            email=user["email"],
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/conversations")
async def list_conversations(user: dict = Depends(get_or_create_user)):
    resolved_user_id = str(user["id"]) if user["id"] else ""
    items = sorted(
        [
            c
            for c in _conversations.values()
            if not resolved_user_id or c.user_id == resolved_user_id
        ],
        key=lambda c: c.updated_at,
        reverse=True,
    )
    return {
        "success": True,
        "data": {
            "conversations": [
                {
                    "id": c.id,
                    "user_id": c.user_id,
                    "email": c.email,
                    "title": c.title,
                    "message_count": len(c.messages),
                    "created_at": c.created_at,
                    "updated_at": c.updated_at,
                }
                for c in items[:50]
            ]
        },
    }


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str, user: dict = Depends(get_or_create_user)
):
    conv = _conversations.get(conversation_id)
    if not conv:
        return {"success": False, "error": "Conversation not found"}
    resolved_user_id = str(user["id"]) if user["id"] else ""
    if resolved_user_id and conv.user_id and conv.user_id != resolved_user_id:
        return {"success": False, "error": "Conversation not found"}
    return {
        "success": True,
        "data": {
            "id": conv.id,
            "user_id": conv.user_id,
            "email": conv.email,
            "title": conv.title,
            "messages": conv.messages,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
        },
    }


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str, user: dict = Depends(get_or_create_user)
):
    if conversation_id in _conversations:
        resolved_user_id = str(user["id"]) if user["id"] else ""
        conv = _conversations[conversation_id]
        if resolved_user_id and conv.user_id and conv.user_id != resolved_user_id:
            return {"success": False, "error": "Conversation not found"}
        del _conversations[conversation_id]
        return {"success": True, "message": "Conversation deleted"}
    return {"success": False, "error": "Conversation not found"}
