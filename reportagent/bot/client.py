"""Feishu SDK send/reply operations."""
import json
import logging
import os

import lark_oapi as lark
from reportagent.utils.config import PROJECT_ROOT

logger = logging.getLogger(__name__)
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

def _client():
    return lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

def send_text(open_id, text):
    from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
    from lark_oapi.api.im.v1.model.create_message_request_body import CreateMessageRequestBody
    c = json.dumps({"text": text}, ensure_ascii=False)
    req = CreateMessageRequest.builder().receive_id_type("open_id").request_body(
        CreateMessageRequestBody.builder().receive_id(open_id).msg_type("text").content(c).build()
    ).build()
    try:
        r = _client().im.v1.message.create(req)
        if r.success(): logger.info("Sent")
        else: logger.error("Send failed: code=%s message=%s", r.code, r.msg)
    except Exception as e: logger.error(f"Send err: {e}")

def reply_msg(message_id, text):
    from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
    from lark_oapi.api.im.v1.model.reply_message_request_body import ReplyMessageRequestBody
    c = json.dumps({"text": text}, ensure_ascii=False)
    req = ReplyMessageRequest.builder().message_id(message_id).request_body(
        ReplyMessageRequestBody.builder().msg_type("text").content(c).build()
    ).build()
    try:
        r = _client().im.v1.message.reply(req)
        if r.success(): logger.info("Replied")
    except Exception as e: logger.error(f"Reply err: {e}")

def send_chat(chat_id, text):
    from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
    from lark_oapi.api.im.v1.model.create_message_request_body import CreateMessageRequestBody
    c = json.dumps({"text": text}, ensure_ascii=False)
    req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
        CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("text").content(c).build()
    ).build()
    try:
        r = _client().im.v1.message.create(req)
        if r.success(): logger.info("Sent to chat")
    except Exception as e: logger.error(f"Send chat err: {e}")

def react(message_id, emoji="OK"):
    from lark_oapi.api.im.v1.model.create_message_reaction_request import CreateMessageReactionRequest
    from lark_oapi.api.im.v1.model.create_message_reaction_request_body import CreateMessageReactionRequestBody
    from lark_oapi.api.im.v1.model.emoji import Emoji
    try:
        e = Emoji.builder().emoji_type(emoji).build()
        b = CreateMessageReactionRequestBody.builder().reaction_type(e).build()
        req = CreateMessageReactionRequest.builder().message_id(message_id).request_body(b).build()
        r = _client().im.v1.message_reaction.create(req)
        if r.success(): logger.info(f"Reacted {emoji}")
    except Exception as e: logger.warning(f"React err: {e}")
