"""
Feishu (Lark) Bot Service — 长连接模式
使用 lark-oapi SDK WebSocket 长连接，无需公网回调 URL
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Callable

from reportagent.utils.config import get_config

logger = logging.getLogger(__name__)

# ── State ──
_ws_client = None
_running = False


def _get_feishu_config() -> dict:
    return get_config("feishu", default={})


def _get_credentials() -> tuple[str, str]:
    cfg = _get_feishu_config()
    app_id = os.environ.get(cfg.get("app_id_env", "FEISHU_APP_ID"), "")
    app_secret = os.environ.get(cfg.get("app_secret_env", "FEISHU_APP_SECRET"), "")
    return app_id, app_secret


def is_configured() -> bool:
    app_id, app_secret = _get_credentials()
    return bool(app_id and app_secret)


# ── Message Sending (REST API) ──


def _build_client():
    import lark_oapi as lark

    app_id, app_secret = _get_credentials()
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


def send_text_message(open_id: str, text: str) -> bool:
    """Send a text message to a Feishu user by open_id."""
    from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
    from lark_oapi.api.im.v1.model.create_message_request_body import (
        CreateMessageRequestBody,
    )

    client = _build_client()
    content = json.dumps({"text": text}, ensure_ascii=False)

    request = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type("text")
            .content(content)
            .build()
        )
        .build()
    )

    try:
        response = client.im.v1.message.create(request)
        if response.success():
            logger.info(f"Message sent to {open_id}")
            return True
        else:
            logger.error(f"Send failed: code={response.code}, msg={response.msg}")
            return False
    except Exception as e:
        logger.error(f"Send error: {e}")
        return False


def send_card_message(open_id: str, card: dict) -> bool:
    """Send an interactive card to a Feishu user."""
    import lark_oapi as lark

    client = _build_client()
    content = json.dumps(card, ensure_ascii=False)

    request = (
        lark.im.v1.message.CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(
            lark.im.v1.message.CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type("interactive")
            .content(content)
            .build()
        )
        .build()
    )

    try:
        response = client.im.v1.message.create(request)
        if response.success():
            logger.info(f"Card sent to {open_id}")
            return True
        else:
            logger.error(f"Send card failed: code={response.code}, msg={response.msg}")
            return False
    except Exception as e:
        logger.error(f"Send card error: {e}")
        return False


def reply_message(message_id: str, text: str) -> bool:
    """Reply to a specific message (used for group chats)."""
    from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
    from lark_oapi.api.im.v1.model.reply_message_request_body import (
        ReplyMessageRequestBody,
    )

    client = _build_client()
    content = json.dumps({"text": text}, ensure_ascii=False)
    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder().msg_type("text").content(content).build()
        )
        .build()
    )

    try:
        response = client.im.v1.message.reply(request)
        if response.success():
            logger.info(f"Replied to {message_id}")
            return True
        else:
            logger.error(f"Reply failed: code={response.code}, msg={response.msg}")
            return False
    except Exception as e:
        logger.error(f"Reply error: {e}")
        return False


def send_to_chat(chat_id: str, text: str) -> bool:
    """Send a text message to a chat_id (alternative to open_id)."""
    from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
    from lark_oapi.api.im.v1.model.create_message_request_body import (
        CreateMessageRequestBody,
    )

    client = _build_client()
    content = json.dumps({"text": text}, ensure_ascii=False)
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(content)
            .build()
        )
        .build()
    )

    try:
        response = client.im.v1.message.create(request)
        if response.success():
            logger.info(f"Sent to chat {chat_id}")
            return True
        else:
            logger.error(
                f"Send to chat failed: code={response.code}, msg={response.msg}"
            )
            return False
    except Exception as e:
        logger.error(f"Send to chat error: {e}")
        return False


# ── Message Reactions (Emoji) ──


def add_reaction(message_id: str, emoji_type: str = "THUMBSUP") -> bool:
    """Add an emoji reaction to a Feishu message.

    Args:
        message_id: The message ID to react to.
        emoji_type: Emoji key (e.g. 'THUMBSUP', 'OK', 'EYES', 'HEART', 'LAUGH').
                     Use emoji_type format, NOT the emoji character itself.

    Returns True if successful.
    """
    from lark_oapi.api.im.v1.model.create_message_reaction_request import (
        CreateMessageReactionRequest,
    )
    from lark_oapi.api.im.v1.model.create_message_reaction_request_body import (
        CreateMessageReactionRequestBody,
    )
    from lark_oapi.api.im.v1.model.emoji import Emoji

    client = _build_client()
    emoji = Emoji.builder().emoji_type(emoji_type).build()
    request = (
        CreateMessageReactionRequest.builder()
        .message_id(message_id)
        .request_body(
            CreateMessageReactionRequestBody.builder().reaction_type(emoji).build()
        )
        .build()
    )

    try:
        response = client.im.v1.message_reaction.create(request)
        if response.success():
            logger.info(f"Reaction {emoji_type} added to {message_id}")
            return True
        else:
            logger.error(f"Reaction failed: code={response.code}, msg={response.msg}")
            return False
    except Exception as e:
        logger.error(f"Reaction error: {e}")
        return False


# ── Long Connection (WebSocket) ──


def start_long_connection(handler: Callable | None = None) -> bool:
    """Start the Feishu long connection WebSocket client.

    Args:
        handler: Callback(dict) for incoming messages.
                 dict keys: sender_id (open_id), message_id, text, chat_type

    Returns True if started.
    """
    global _ws_client, _running

    app_id, app_secret = _get_credentials()
    cfg = _get_feishu_config()
    verification_token = os.environ.get(
        cfg.get("verification_token_env", "FEISHU_VERIFICATION_TOKEN"), ""
    )

    if not app_id or not app_secret:
        logger.warning("Feishu credentials not configured, long connection skipped")
        return False

    if _running:
        logger.info("Feishu long connection already running")
        return True

    # Start in daemon thread — all SDK imports happen inside so the
    # module-level asyncio loop is created in the thread context.
    def run_ws():
        global _running, _ws_client
        from lark_oapi.ws import Client as WSClient
        from lark_oapi.event import dispatcher_handler as dh

        # ── Event handler ──
        def on_message(data):
            """Handle im.message.receive_v1 event."""
            try:
                event_data = data.event
                message = event_data.message
                sender = event_data.sender

                content_str = message.content or "{}"
                try:
                    content = json.loads(content_str)
                    text = content.get("text", "")
                except json.JSONDecodeError:
                    text = content_str

                sender_id = (
                    sender.sender_id.open_id if sender and sender.sender_id else ""
                )
                msg_id = message.message_id or ""
                chat_type = message.chat_type or ""
                chat_id = getattr(message, "chat_id", "") or ""

                if not text.strip():
                    return

                logger.info(
                    f"Feishu WS received: [{chat_type}] {text[:80]} from {sender_id} (chat={chat_id})"
                )

                if handler:
                    handler(
                        {
                            "sender_id": sender_id,
                            "message_id": msg_id,
                            "text": text,
                            "chat_type": chat_type,
                            "chat_id": chat_id,
                        }
                    )

            except Exception as e:
                logger.error(f"Message event handler error: {e}", exc_info=True)

        # Build event dispatcher
        event_handler = (
            dh.EventDispatcherHandlerBuilder(
                encrypt_key="", verification_token=verification_token
            )
            .register_p2_im_message_receive_v1(on_message)
            .build()
        )

        # Build WS client (in thread context — asyncio.Lock is created here)
        _ws_client = WSClient(
            app_id=app_id,
            app_secret=app_secret,
            event_handler=event_handler,
        )

        _running = True
        logger.info("Feishu long connection starting...")
        try:
            _ws_client.start()
        except Exception as e:
            logger.error(f"Feishu WS error: {e}")
        finally:
            _running = False
            logger.info("Feishu long connection stopped")

    thread = threading.Thread(target=run_ws, daemon=True, name="feishu-ws")
    thread.start()
    logger.info("Feishu long connection thread started")
    return True


def stop_long_connection():
    """Stop the Feishu long connection."""
    global _running
    _running = False
    logger.info("Feishu long connection stopped")


def is_running() -> bool:
    return _running


# ── Simple Command Handler ──


def _handle_simple_command(text: str) -> str | None:
    text = text.strip()
    # Strip @mention prefixes (e.g., "@_user_1 /stats" → "/stats")
    import re

    text = re.sub(r"@\S+\s*", "", text).strip()

    if text in ("/help", "/start"):
        return (
            "📊 **AlphaReport 指令帮助**\n\n"
            "• `/stats` — 查看研报库统计\n"
            "• `/search <关键词>` — 搜索研报\n"
            "• `/recent` — 最近添加的研报\n"
            "• `/help` — 显示此帮助\n\n"
            "💡 直接发送问题，Claude Code Agent 为您分析"
        )

    if text == "/stats":
        try:
            from reportagent.db.engine import get_session_factory
            from reportagent.db.repository import ReportRepository

            factory = get_session_factory()
            session = factory()
            try:
                repo = ReportRepository(session)
                stats = repo.get_stats()
                return (
                    f"📚 **研报库统计**\n\n"
                    f"• 总计：{stats['total_reports']} 篇\n"
                    f"• 含全文：{stats['with_full_text']} 篇\n"
                    f"• 仅摘要：{stats['without_full_text']} 篇"
                )
            finally:
                session.close()
        except Exception as e:
            return f"查询统计失败: {e}"

    if text.startswith("/search"):
        query = text.replace("/search", "").strip()
        if not query:
            return "请提供搜索关键词，例如：`/search 因子模型`"
        try:
            from reportagent.db.engine import get_session_factory
            from reportagent.db.repository import ReportRepository
            from reportagent.models.schemas import ReportListParams

            factory = get_session_factory()
            session = factory()
            try:
                repo = ReportRepository(session)
                params = ReportListParams(search=query, limit=5)
                reports, total = repo.list_reports(params)
                if not reports:
                    return f"未找到与「{query}」相关的研报"
                lines = [f"🔍 搜索「{query}」— 共 {total} 篇，前 5 篇：\n"]
                for i, r in enumerate(reports, 1):
                    lines.append(f"{i}. **{r.title[:80]}** ({r.source})")
                return "\n".join(lines)
            finally:
                session.close()
        except Exception as e:
            return f"搜索失败: {e}"

    if text == "/recent":
        try:
            from reportagent.db.engine import get_session_factory
            from reportagent.db.repository import ReportRepository
            from reportagent.models.schemas import ReportListParams

            factory = get_session_factory()
            session = factory()
            try:
                repo = ReportRepository(session)
                params = ReportListParams(sort_by="created_at", limit=5)
                reports, total = repo.list_reports(params)
                if not reports:
                    return "研报库为空"
                lines = ["📄 **最近添加的研报**\n"]
                for i, r in enumerate(reports, 1):
                    date = r.created_at.strftime("%m-%d") if r.created_at else ""
                    lines.append(f"{i}. **{r.title[:60]}** — {date}")
                return "\n".join(lines)
            finally:
                session.close()
        except Exception as e:
            return f"查询失败: {e}"

    return None  # → queued for Claude Code


# ── Card Builders ──


def build_report_card(reports: list[dict]) -> dict:
    elements = []
    for i, r in enumerate(reports[:10]):
        title = r.get("title", "Unknown")[:100]
        source = r.get("source", "")
        topics = ", ".join(r.get("topics", []))
        report_id = r.get("id", i)
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{i + 1}. [{report_id}] {title}**\n来源：{source} | 主题：{topics}",
                },
            }
        )
        if i < min(len(reports[:10]), 10) - 1:
            elements.append({"tag": "hr"})

    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"📊 研报列表（共 {len(reports)} 篇）",
            },
            "template": "blue",
        },
        "elements": elements,
    }


def build_analysis_card(report: dict) -> dict:
    title = report.get("title", "Unknown")[:100]
    analysis = report.get("analysis", {})
    assessment = analysis.get("assessment", {}) if isinstance(analysis, dict) else {}

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**📄 {title}**"}},
        {"tag": "hr"},
    ]

    if analysis.get("summary"):
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**总结**\n{analysis['summary'][:500]}",
                },
            }
        )

    score = assessment.get("overall_quality_score", "")
    if score:
        stars = "⭐" * min(5, int(float(score) + 0.5))
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**综合评分**：{stars} ({score}/5)",
                },
            }
        )

    return {
        "header": {
            "title": {"tag": "plain_text", "content": "🔬 研报分析结果"},
            "template": "blue",
        },
        "elements": elements,
    }
