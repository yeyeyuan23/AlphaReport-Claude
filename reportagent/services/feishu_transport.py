"""One SDK client factory, request builder and acknowledgement policy for Feishu."""

from __future__ import annotations

import json
import os

from reportagent.utils.config import get_config

CHUNK_BYTES = 7000


def _get_credentials() -> tuple[str, str]:
    config = get_config("feishu", default={})
    return (
        os.environ.get(config.get("app_id_env", "FEISHU_APP_ID"), ""),
        os.environ.get(config.get("app_secret_env", "FEISHU_APP_SECRET"), ""),
    )


def is_configured() -> bool:
    app_id, app_secret = _get_credentials()
    return bool(app_id and app_secret)


def build_client(app_id=None, app_secret=None):
    import lark_oapi as lark

    configured_id, configured_secret = _get_credentials()
    if app_id is None:
        app_id = configured_id
    if app_secret is None:
        app_secret = configured_secret
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


def text_chunks(text: str, limit: int = CHUNK_BYTES) -> list[str]:
    """Keep every Unicode character within a conservative UTF-8 content budget."""
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


def send_payload(
    client,
    kind,
    content,
    receive_id,
    *,
    receive_type="chat_id",
    reply_to=None,
    request_id=None,
) -> str:
    """A successful API status without a real message ID is not a delivery receipt."""
    from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
    from lark_oapi.api.im.v1.model.create_message_request_body import (
        CreateMessageRequestBody,
    )
    from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
    from lark_oapi.api.im.v1.model.reply_message_request_body import (
        ReplyMessageRequestBody,
    )

    if reply_to:
        body = ReplyMessageRequestBody.builder().msg_type(kind).content(content)
        if request_id:
            body.uuid(request_id)
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
            body.uuid(request_id)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_type)
            .request_body(body.build())
            .build()
        )
        response = client.im.v1.message.create(request)
    data = getattr(response, "data", None)
    message_id = getattr(data, "message_id", None)
    if (
        not response.success()
        or not isinstance(message_id, str)
        or not message_id.strip()
    ):
        raise RuntimeError(
            f"Feishu delivery failed: code={getattr(response, 'code', 'unknown')}"
        )
    return message_id


def send_complete_text(text, sender) -> bool:
    """Boolean adapter for report workflows; stop at the first failed text part."""
    parts = text_chunks(text)
    if not parts:
        return False
    try:
        for part in parts:
            if not sender(part):
                return False
        return True
    except Exception:
        return False


def send_card_with_fallback(
    client, card, text, receive_id, *, receive_type="chat_id", text_sender=None
) -> bool:
    try:
        send_payload(
            client,
            "interactive",
            json.dumps(card, ensure_ascii=False),
            receive_id,
            receive_type=receive_type,
        )
        return True
    except Exception:
        if text_sender is None:

            def text_sender(part):
                return send_payload(
                    client,
                    "text",
                    json.dumps({"text": part}, ensure_ascii=False),
                    receive_id,
                    receive_type=receive_type,
                )

        return send_complete_text(text, text_sender)
