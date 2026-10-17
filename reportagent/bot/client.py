"""Feishu SDK send/reply operations."""

import logging


logger = logging.getLogger(__name__)


def _client():
    from reportagent.services.feishu_transport import build_client

    return build_client()


def _send(text, receive_id, *, receive_type="chat_id", reply_to=None):
    from reportagent.services.feishu_replies import deliver_response

    try:
        return deliver_response(
            _client(), text, receive_id, receive_type=receive_type, reply_to=reply_to
        )
    except Exception:
        logger.exception("Feishu reply was not fully delivered")
        return []


def send_text(open_id, text):
    return _send(text, open_id, receive_type="open_id")


def reply_msg(message_id, text):
    return _send(text, "", reply_to=message_id)


def send_chat(chat_id, text):
    return _send(text, chat_id)


def react(message_id, emoji="OK"):
    from lark_oapi.api.im.v1.model.create_message_reaction_request import (
        CreateMessageReactionRequest,
    )
    from lark_oapi.api.im.v1.model.create_message_reaction_request_body import (
        CreateMessageReactionRequestBody,
    )
    from lark_oapi.api.im.v1.model.emoji import Emoji

    try:
        e = Emoji.builder().emoji_type(emoji).build()
        b = CreateMessageReactionRequestBody.builder().reaction_type(e).build()
        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(b)
            .build()
        )
        r = _client().im.v1.message_reaction.create(req)
        if r.success():
            logger.info(f"Reacted {emoji}")
    except Exception as e:
        logger.warning(f"React err: {e}")
