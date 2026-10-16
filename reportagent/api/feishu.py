"""Optional authenticated HTTP transport for the Feishu bot."""
import json
import logging
import os
import time
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from reportagent.utils.config import PROJECT_ROOT
from reportagent.services.feishu_security import http_callback_enabled, secret_matches, sender_allowed
from reportagent.services.feishu_inbox import enqueue, drain

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/feishu", tags=["feishu"])

@router.post("/callback")
async def feishu_callback(request: Request):
    """Optional plaintext webhook. WebSocket is the default transport."""
    if not http_callback_enabled():
        raise HTTPException(404, "HTTP callback is disabled")
    expected = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
    if not expected or not os.environ.get("FEISHU_INBOX_TOKEN", ""):
        raise HTTPException(503, "HTTP callback credentials are not configured")
    body = await request.body()
    try: data = json.loads(body)
    except json.JSONDecodeError: raise HTTPException(400, "Invalid JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected a JSON object")
    header = data.get("header") or {}
    if not isinstance(header, dict):
        raise HTTPException(400, "Invalid event header")
    supplied = header.get("token") or data.get("token") or ""
    if not isinstance(supplied, str) or not secret_matches(expected, supplied):
        raise HTTPException(403, "Invalid verification token")

    challenge = data.get("challenge")
    if challenge:
        return JSONResponse({"challenge": challenge})

    try:
        evt = (data.get("event") or {})
        msg = (evt.get("message") or {})
        sender_id = ((evt.get("sender") or {}).get("sender_id") or {}).get("open_id", "")
        text = json.loads(msg.get("content", "{}")).get("text", "")
    except Exception:
        return {"code": 0}

    if text.strip():
        if not sender_allowed(sender_id):
            raise HTTPException(403, "Sender is not authorized")
        if not msg.get("message_id"):
            raise HTTPException(400, "Missing message_id")
        logger.info("Verified webhook event received")
        enqueue(PROJECT_ROOT, {
            "sender_id": sender_id,
            "message_id": msg.get("message_id", ""),
            "text": text,
            "chat_type": msg.get("chat_type", "private"),
            "chat_id": msg.get("chat_id", ""),
            "ts": time.time(),
        })

    return {"code": 0}

@router.post("/inbox")
def feishu_inbox(request: Request):
    """Only the bot holding the separate inbox secret may consume events."""
    if not http_callback_enabled():
        raise HTTPException(404, "HTTP callback is disabled")
    expected = os.environ.get("FEISHU_INBOX_TOKEN", "")
    if not secret_matches(f"Bearer {expected}" if expected else "", request.headers.get("authorization", "")):
        raise HTTPException(403, "Invalid inbox credential")
    return {"success": True, "data": drain(PROJECT_ROOT)}
