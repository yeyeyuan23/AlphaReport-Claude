"""Feishu event loop and user commands. Run with python -m reportagent.bot.feishu."""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

from lark_oapi.ws import Client as WSClient
from lark_oapi.event import dispatcher_handler as dh
from reportagent.utils.config import PROJECT_ROOT
from reportagent.db.engine import init_db
from reportagent.services.feishu_security import http_callback_enabled, sender_allowed
from reportagent.bot.client import send_text, send_chat, reply_msg, react
from reportagent.bot.claude import ask_claude
from reportagent.bot.sessions import del_session

logger = logging.getLogger(__name__)
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
_report_lock = threading.Lock()

def run_report_job(arxiv_id, chat_id):
    """Run the bounded report pipeline outside the WebSocket callback."""
    try:
        log_path = PROJECT_ROOT / "data" / "report-jobs.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            result = subprocess.run(
                [sys.executable, "-u", "-m", "reportagent.cli.report",
                 "--arxiv-id", arxiv_id, "--chat-id", chat_id],
                cwd=str(PROJECT_ROOT), stdout=log, stderr=log, timeout=1300,
            )
        if result.returncode:
            send_chat(chat_id, "研报任务失败，未确认推送成功。请查看本机 data/report-jobs.log。")
    except subprocess.TimeoutExpired:
        send_chat(chat_id, "研报任务超时，请查看本机 data/report-jobs.log 后再重试。")
    except Exception:
        logger.exception("Report job failed")
        send_chat(chat_id, "本机研报任务异常，请查看机器人日志。")
    finally:
        _report_lock.release()


def run_daily_job(chat_id):
    try:
        path = PROJECT_ROOT / "data" / "report-jobs.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log:
            result = subprocess.run([
                sys.executable, "-u", "-m", "reportagent.cli.daily_feishu_push",
                "--receive-ids", chat_id, "--receive-type", "chat_id",
            ], cwd=str(PROJECT_ROOT), stdout=log, stderr=log, timeout=5400)
        if result.returncode:
            send_chat(chat_id, "日报未完整完成，已保存运行记录；可稍后重试。")
    except Exception:
        logger.exception("Daily report job failed")
        send_chat(chat_id, "日报任务未完成，请查看本机 report-jobs.log。")
    finally:
        _report_lock.release()

def handle_cmd(text, chat_id, chat_type):
    text = re.sub(r'<at[^>]*>.*?</at>', '', text.strip())
    text = re.sub(r'@\S+\s*', '', text).strip()

    if text in ("/help", "/start"):
        return "📊 AlphaReport 指令\n\n/report <arXiv编号或链接>：下载全文、分析并推送到当前私聊\n/daily：采集、评估最多12篇候选，推送分层日报\n/stats /search <词> /recent /chatid /clear /help\n\n💬 直接发问题，模型为你解答"

    if text == "/daily":
        if chat_type != "p2p":
            return "请在机器人私聊中使用 /daily。"
        if not _report_lock.acquire(blocking=False):
            return "已有研报任务正在处理，请等待完成后再提交。"
        try:
            threading.Thread(target=run_daily_job, args=(chat_id,), daemon=True).start()
        except Exception:
            _report_lock.release()
            raise
        return "开始生成日报：先检查来源和相关性，再评估最多12篇全文，完成后推送到当前私聊。"

    if text == "/report" or text.startswith("/report "):
        if chat_type != "p2p":
            return "请在机器人私聊中使用 /report。"
        from reportagent.services.one_report import normalize_arxiv_id
        try:
            arxiv_id = normalize_arxiv_id(text.removeprefix("/report").strip())
        except ValueError:
            return "用法：/report <arXiv编号>，例如 /report 2005.13665"
        if not _report_lock.acquire(blocking=False):
            return "已有一篇研报正在处理，请等待完成后再提交。"
        try:
            threading.Thread(target=run_report_job, args=(arxiv_id, chat_id), daemon=True).start()
        except Exception:
            _report_lock.release()
            raise
        return f"开始采集和分析 arXiv:{arxiv_id}，完成后推送到当前私聊，通常需要几分钟。"

    if text == "/stats":
        try:
            from reportagent.db.engine import get_session_factory
            from reportagent.db.repository import ReportRepository
            s = get_session_factory()()
            st = ReportRepository(s).get_stats(); s.close()
            return f"📚 研报库：{st['total_reports']} 篇 | 含全文 {st['with_full_text']} | 仅摘要 {st['without_full_text']}"
        except Exception as e: return f"查询失败: {e}"

    if text.startswith("/search"):
        q = text.replace("/search", "").strip()
        if not q: return "用法：/search <关键词>"
        try:
            from reportagent.db.engine import get_session_factory
            from reportagent.db.repository import ReportRepository
            from reportagent.models.schemas import ReportListParams
            s = get_session_factory()()
            reps, total = ReportRepository(s).list_reports(ReportListParams(search=q, limit=5))
            s.close()
            if not reps: return f"未找到「{q}」"
            return "\n".join([f"{i}. {r.title[:70]} ({r.source})" for i, r in enumerate(reps, 1)])
        except Exception as e: return f"搜索失败: {e}"

    if text == "/recent":
        try:
            from reportagent.db.engine import get_session_factory
            from reportagent.db.repository import ReportRepository
            from reportagent.models.schemas import ReportListParams
            s = get_session_factory()()
            reps, _ = ReportRepository(s).list_reports(ReportListParams(sort_by="created_at", limit=5))
            s.close()
            if not reps: return "研报库为空"
            return "\n".join([f"{i}. {r.title[:60]} — {r.created_at.strftime('%m-%d') if r.created_at else ''}" for i, r in enumerate(reps, 1)])
        except Exception as e: return f"查询失败: {e}"

    if text == "/chatid":
        t = "群聊" if chat_type == "group" else "对话"
        return f"📋 当前{t} ID: `{chat_id}`"

    if text == "/clear":
        del_session(chat_id)
        return "🧹 已清除会话记忆"

    return None

def on_message(data):
    try:
        msg = data.event.message
        sender = data.event.sender
        mid = msg.message_id or ""
        cid = getattr(msg, "chat_id", "") or ""
        ct = msg.chat_type or ""
        text = json.loads(msg.content or "{}").get("text", "")
        # open_id for private chats; chat_id for groups
        open_id = sender.sender_id.open_id if sender and sender.sender_id else ""
        if not sender_allowed(open_id):
            logger.warning("Ignored unauthorized sender: %s chat=%s", open_id, cid)
            return
        if not text.strip(): return

        logger.info(f"[{ct}] {text[:80]}")
        react(mid, "THUMBSUP")   # 👍 已收到

        # Command?
        reply = handle_cmd(text, cid, ct)
        if reply:
            if ct == "group":
                if mid: reply_msg(mid, reply)
                else: send_chat(cid, reply)
            else:
                send_text(open_id, reply)
            react(mid, "OK")       # ✅ 完成
            return

        # Claude Code (async)
        if ct == "group":
            if mid: reply_msg(mid, "🤔 正在处理...")
            else: send_chat(cid, "🤔 正在处理...")
        else:
            send_text(open_id, "🤔 正在处理...")

        def process():
            ans = ask_claude(text, cid)
            if ct == "group":
                if mid: reply_msg(mid, ans)
                else: send_chat(cid, ans)
            else:
                send_text(open_id, ans)
        threading.Thread(target=process, daemon=True).start()

    except Exception as e:
        logger.error(f"Handler: {e}", exc_info=True)

def make_inbox_request():
    import urllib.request
    port = int(os.environ.get("ALPHAREPORT_PORT", "8000"))
    token = os.environ.get("FEISHU_INBOX_TOKEN", "")
    return urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/feishu/inbox", method="POST",
                                  headers={"Authorization": f"Bearer {token}"})

def main():
    if not APP_ID or not APP_SECRET:
        logger.error("FEISHU_APP_ID/APP_SECRET not set"); sys.exit(1)
    init_db()
    logger.info(f"Bot starting, project={PROJECT_ROOT}")

    # ── Webhook poller: reliable fallback when WebSocket drops ──
    import urllib.request as ur

    def poll_webhook():
        while True:
            try:
                req = make_inbox_request()
                with ur.urlopen(req, timeout=3) as resp:
                    msgs = json.loads(resp.read()).get("data", [])
                    for m in msgs:
                        logger.info(f"Webhook: {m.get('text','')[:80]}")
                        on_message_raw(m)
            except Exception:
                pass
            time.sleep(1)

    def on_message_raw(m):
        """Process message from webhook inbox."""
        mid = m.get("message_id", "")
        cid = m.get("chat_id", "") or m.get("sender_id", "")
        ct = m.get("chat_type", "private")
        open_id = m.get("sender_id", "")
        text = m.get("text", "")
        if not sender_allowed(open_id):
            return
        if not text.strip(): return
        react(mid, "THUMBSUP")
        reply = handle_cmd(text, cid, ct)
        if reply:
            if ct == "group":
                if mid: reply_msg(mid, reply)
                else: send_chat(cid, reply)
            else:
                send_text(open_id, reply)
            react(mid, "OK")
            return
        # Async Claude
        if ct == "group":
            if mid: reply_msg(mid, "🤔 正在处理...")
            else: send_chat(cid, "🤔 正在处理...")
        else:
            send_text(open_id, "🤔 正在处理...")
        def process():
            ans = ask_claude(text, cid)
            if ct == "group":
                if mid: reply_msg(mid, ans)
                else: send_chat(cid, ans)
            else:
                send_text(open_id, ans)
        threading.Thread(target=process, daemon=True).start()

    if http_callback_enabled() and os.environ.get("FEISHU_INBOX_TOKEN"):
        threading.Thread(target=poll_webhook, daemon=True).start()

    # ── WebSocket: primary path ──
    eh = dh.EventDispatcherHandlerBuilder(encrypt_key="", verification_token="") \
        .register_p2_im_message_receive_v1(on_message) \
        .build()

    ws = WSClient(app_id=APP_ID, app_secret=APP_SECRET, event_handler=eh)

    while True:
        try:
            logger.info("Connecting to Feishu WebSocket...")
            ws.start()
        except KeyboardInterrupt: break
        except Exception as e: logger.error(f"WS: {e}")
        logger.info("Reconnecting in 5s...")
        time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    main()
