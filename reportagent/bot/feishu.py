"""Feishu event loop and user commands. Run with python -m reportagent.bot.feishu."""

import json
import asyncio
import logging
from collections import deque
from dataclasses import dataclass
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from lark_oapi.ws import Client as WSClient
from lark_oapi.event import dispatcher_handler as dh
from reportagent.utils.config import PROJECT_ROOT
from reportagent.services.public_url import get_web_base_url, get_library_url
from reportagent.db.engine import init_db
from reportagent.services.feishu_security import http_callback_enabled, sender_allowed
from reportagent.bot.client import send_text, send_chat, reply_msg, react
from reportagent.bot.claude import ask_claude_outcome
from reportagent.bot.sessions import (
    del_session,
    get_reset_generation,
    reserve_message,
    finish_message,
    clear_message,
)

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
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "reportagent.cli.report",
                    "--arxiv-id",
                    arxiv_id,
                    "--chat-id",
                    chat_id,
                ],
                cwd=str(PROJECT_ROOT),
                stdout=log,
                stderr=log,
                timeout=1300,
            )
        if result.returncode:
            send_chat(
                chat_id,
                "研报任务失败，未确认推送成功。请查看本机 data/report-jobs.log。",
            )
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
            result = subprocess.run(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "reportagent.cli.daily_feishu_push",
                    "--receive-ids",
                    chat_id,
                    "--receive-type",
                    "chat_id",
                ],
                cwd=str(PROJECT_ROOT),
                stdout=log,
                stderr=log,
            )
        if result.returncode:
            send_chat(chat_id, "日报未完整完成，已保存运行记录；可稍后重试。")
    except Exception:
        logger.exception("Daily report job failed")
        send_chat(chat_id, "日报任务未完成，请查看本机 report-jobs.log。")
    finally:
        _report_lock.release()


def run_weekly_job(chat_id):
    """Reuse the weekly CLI outside the event callback and deliver to this chat."""
    try:
        from reportagent.services.weekly_report import (
            get_weekly_range,
            get_weekly_papers,
        )

        start, end, first, last = get_weekly_range()
        if not get_weekly_papers(start, end):
            send_chat(
                chat_id,
                f"当天北京时间08:00前168小时（{first} 至 {last}）库中暂无符合公告时间窗口的资料，可先采集后再试。\n\n"
                f"[研报库首页]({get_library_url()})",
            )
            return
        path = PROJECT_ROOT / "data" / "report-jobs.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log:
            result = subprocess.run(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "reportagent.cli.weekly_report",
                    "--receive-ids",
                    chat_id,
                    "--receive-type",
                    "chat_id",
                ],
                cwd=str(PROJECT_ROOT),
                stdout=log,
                stderr=log,
            )
        if result.returncode:
            send_chat(chat_id, "周报未完整完成，部分评估或投递可能未完成，可稍后重试。")
    except subprocess.TimeoutExpired:
        logger.warning("Weekly report job timed out")
        send_chat(chat_id, "周报生成超时，可稍后重试。")
    except Exception:
        logger.exception("Weekly report job failed")
        send_chat(chat_id, "周报任务未完成，可稍后重试。")
    finally:
        _report_lock.release()


def _report_list(reports, total, title):
    base = get_web_base_url()
    sources = {
        "arxiv": "arXiv",
        "eastmoney": "东方财富",
        "bigquant": "BigQuant",
        "local_pdf": "本地 PDF",
    }
    entries = [f"{title}\n共 {total} 篇，本次展示 {len(reports)} 篇"]
    for i, report in enumerate(reports, 1):
        name = " ".join(report.title.split())
        name = name[:89] + "…" if len(name) > 90 else name
        source = sources.get(report.source, report.source)
        added = (
            report.created_at.strftime("%Y-%m-%d") if report.created_at else "日期未知"
        )
        entries.append(
            f"{i}. {name}\n来源：{source} · 入库：{added}\n网页详情：{base}/library/{report.id}"
        )
    return "\n\n".join(entries)


def normalize_command(text):
    text = re.sub(r"<at[^>]*>.*?</at>", "", text.strip())
    return re.sub(r"@\S+\s*", "", text).strip()


def command_name(text):
    """Classify normalized commands without changing ordinary slash-prefixed text."""
    if text in {
        "/help",
        "/start",
        "/stats",
        "/recent",
        "/chatid",
        "/clear",
        "/daily",
        "/search",
        "/report",
    }:
        return text
    if text.startswith("/search "):
        return "/search"
    if text.startswith("/report "):
        return "/report"
    if re.match(r"^/weekly(?:\s|$)", text):
        return "/weekly"
    return None


def handle_cmd(text, chat_id, chat_type):
    text = normalize_command(text)
    command = command_name(text)

    if command in ("/help", "/start"):
        return (
            "📊 AlphaReport 指令\n\n"
            "/report <arXiv编号或链接>：分析单篇论文并推送到当前私聊\n"
            "/daily：汇总当天北京时间08:00前24小时公开发布的论文\n"
            "/weekly：汇总当天北京时间08:00前168小时的库中资料\n\n"
            "/stats：查看研报库统计\n"
            "/search <关键词>：搜索研报，最多显示 5 篇\n"
            "/recent：查看最近入库的 5 篇研报\n"
            "/chatid：查看当前会话 ID\n"
            "/clear：清除聊天记忆（不删除研报）\n"
            "/help：显示帮助\n\n"
            "💬 直接发问题，也可以讨论论文。"
        )

    if command == "/weekly":
        if chat_type != "p2p":
            return "请在机器人私聊中使用 /weekly。"
        if text != "/weekly":
            return "用法：/weekly（无需参数，汇总当天北京时间08:00前168小时）。"
        if not _report_lock.acquire(blocking=False):
            return "已有研报任务正在处理，请等待完成后再提交。"
        try:
            threading.Thread(
                target=run_weekly_job, args=(chat_id,), daemon=True
            ).start()
        except Exception:
            _report_lock.release()
            raise
        return "开始生成当天北京时间08:00前168小时的周报：使用库中资料，刷新相关性并评估符合条件的论文，完成后推送到当前私聊。"

    if command == "/daily":
        if chat_type != "p2p":
            return "请在机器人私聊中使用 /daily。"
        if not _report_lock.acquire(blocking=False):
            return "已有研报任务正在处理，请等待完成后再提交。"
        try:
            threading.Thread(target=run_daily_job, args=(chat_id,), daemon=True).start()
        except Exception:
            _report_lock.release()
            raise
        return "开始生成日报：使用库中当天北京时间08:00前24小时公开发布且有来源时间证据的资料，刷新相关性并评估符合条件的论文，完成后推送到当前私聊。"

    if command == "/report":
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
            threading.Thread(
                target=run_report_job, args=(arxiv_id, chat_id), daemon=True
            ).start()
        except Exception:
            _report_lock.release()
            raise
        return (
            f"开始采集和分析 arXiv:{arxiv_id}，完成后推送到当前私聊，通常需要几分钟。"
        )

    if command == "/stats":
        try:
            from reportagent.db.engine import get_session_factory
            from reportagent.db.repository import ReportRepository

            s = get_session_factory()()
            st = ReportRepository(s).get_stats()
            s.close()
            return f"📚 研报库统计\n\n总计：{st['total_reports']} 篇\n含全文：{st['with_full_text']} 篇\n仅摘要：{st['without_full_text']} 篇"
        except Exception:
            logger.exception("Report query failed")
            return "查询失败，请稍后重试。"

    if command == "/search":
        q = text.removeprefix("/search").strip()
        if not q:
            return "用法：/search <关键词>"
        try:
            from reportagent.db.engine import get_session_factory
            from reportagent.db.repository import ReportRepository
            from reportagent.models.schemas import ReportListParams

            s = get_session_factory()()
            reps, total = ReportRepository(s).list_reports(
                ReportListParams(search=q, limit=5)
            )
            s.close()
            if not reps:
                return f"未找到「{q}」"
            return _report_list(reps, total, f"🔍 搜索「{q}」")
        except Exception:
            logger.exception("Report search failed")
            return "搜索失败，请稍后重试。"

    if command == "/recent":
        try:
            from reportagent.db.engine import get_session_factory
            from reportagent.db.repository import ReportRepository
            from reportagent.models.schemas import ReportListParams

            s = get_session_factory()()
            reps, total = ReportRepository(s).list_reports(
                ReportListParams(sort_by="created_at", limit=5)
            )
            s.close()
            if not reps:
                return "研报库为空"
            return _report_list(reps, total, "📄 最近入库")
        except Exception:
            logger.exception("Report query failed")
            return "查询失败，请稍后重试。"

    if command == "/chatid":
        t = "群聊" if chat_type == "group" else "对话"
        return f"📋 当前{t} ID\n\n{chat_id}"

    if command == "/clear":
        del_session(chat_id)
        return "🧹 已清除会话记忆"

    return None


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str
    sender_id: str
    text: str


def _incoming(*, message_id="", chat_id="", chat_type="private", sender_id="", text=""):
    if not all(
        isinstance(value, str)
        for value in (message_id, chat_id, chat_type, sender_id, text)
    ):
        raise ValueError("Invalid incoming message fields")
    return IncomingMessage(
        message_id,
        chat_id or sender_id,
        "group" if chat_type == "group" else "p2p",
        sender_id,
        text,
    )


def _reply(event: IncomingMessage, text: str, *, request_id=None, should_send=None):
    if event.chat_type == "group":
        if event.message_id:
            return reply_msg(
                event.message_id, text, request_id=request_id, should_send=should_send
            )
        return send_chat(
            event.chat_id, text, request_id=request_id, should_send=should_send
        )
    return send_text(
        event.sender_id, text, request_id=request_id, should_send=should_send
    )


class MessageDispatcher:
    """One-process FIFO per reset generation, with durable result replay receipts.

    The short lock only protects admission, active IDs and queue ownership.
    Network/model work never holds it, so a reset can immediately admit a fresh
    generation while old subprocesses wind down. It does not kill subprocesses.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active = set()
        self._queues = {}

    def submit(self, event):
        message_id = event.message_id or "anonymous:" + str(uuid.uuid4())
        text = normalize_command(event.text)
        with self._lock:
            receipt = reserve_message(message_id, event)
            if message_id in self._active or receipt["state"] in {
                "delivered",
                "cancelled",
            }:
                return
            if receipt["generation"] != get_reset_generation(event.chat_id):
                finish_message(message_id, "cancelled")
                return
            if text == "/clear" and receipt["state"] == "retryable":
                answer = "🧹 已清除会话记忆"
                generation = clear_message(message_id, answer)
                if generation is None:
                    return
                receipt.update(generation=generation, answer=answer, state="completed")
            self._active.add(message_id)
            entry = (event, receipt)
            # Commands keep their immediate dispatch behavior. Unknown slash text
            # is still an ordinary model question and goes through the FIFO.
            command = command_name(text)
            immediate = receipt["state"] == "completed" or command is not None
            if not immediate:
                key, start = self._enqueue(entry)
        if immediate:
            try:
                if receipt["state"] == "completed":
                    self._deliver(entry)
                elif self._current(entry):
                    answer = handle_cmd(event.text, event.chat_id, event.chat_type)
                    if answer is None:
                        raise RuntimeError(
                            "Recognized command did not produce a result"
                        )
                    self._complete(entry, answer)
            except Exception:
                logger.exception(
                    "Feishu command failed; incoming message remains retryable"
                )
            finally:
                with self._lock:
                    self._active.discard(message_id)
        elif start:
            self._start(key)

    def _enqueue(self, entry):
        event, receipt = entry
        key = (event.chat_id, receipt["generation"])
        start = key not in self._queues
        self._queues.setdefault(key, deque()).append(entry)
        return key, start

    def _start(self, key):
        # If startup fails, make the affected head retryable and still try to
        # drain any followers admitted while Thread.start was in progress.
        while True:
            try:
                threading.Thread(target=self._drain, args=(key,), daemon=True).start()
                return
            except Exception:
                logger.exception(
                    "Could not start Feishu worker; message can be retried"
                )
                with self._lock:
                    queue = self._queues[key]
                    _, receipt = queue.popleft()
                    self._active.discard(receipt["message_id"])
                    if not queue:
                        del self._queues[key]
                        return

    def _drain(self, key):
        while True:
            with self._lock:
                queue = self._queues[key]
                if not queue:
                    del self._queues[key]
                    return
                entry = queue.popleft()
            try:
                self._process(entry)
            except Exception:
                logger.exception(
                    "Feishu worker failed; incoming message remains retryable"
                )
                try:
                    if self._current(entry):
                        self._send(entry, "处理失败，请稍后重试。", "error")
                except Exception:
                    logger.exception("Could not report failed Feishu worker")
            finally:
                with self._lock:
                    self._active.discard(entry[1]["message_id"])

    @staticmethod
    def _current(entry):
        event, receipt = entry
        if receipt["generation"] == get_reset_generation(event.chat_id):
            return True
        finish_message(receipt["message_id"], "cancelled")
        return False

    @staticmethod
    def _react(event, emoji):
        try:
            react(event.message_id, emoji)
        except Exception:
            logger.exception("Feishu reaction failed")

    @staticmethod
    def _send(entry, text, phase):
        event, receipt = entry
        request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, "alphareport:" + receipt["message_id"] + ":" + phase
            )
        )
        try:
            return _reply(
                event,
                text,
                request_id=request_id,
                should_send=lambda: MessageDispatcher._current(entry),
            )
        except Exception:
            logger.exception("Feishu reply failed; cached result can be redelivered")
            return []

    def _process(self, entry):
        event, receipt = entry
        if not self._current(entry):
            return
        self._react(event, "THUMBSUP")
        self._send(entry, "🤔 正在处理...", "ack")
        # Dequeue-time state is used by the model adapter, while the original
        # reset generation is retained across compaction and resume recovery.
        if not self._current(entry):
            return
        outcome = ask_claude_outcome(
            event.text, event.chat_id, generation=receipt["generation"]
        )
        if outcome.status == "success":
            self._complete(entry, outcome.text)
        elif outcome.status == "cancelled":
            finish_message(receipt["message_id"], "cancelled")
        elif self._current(entry):
            self._send(entry, outcome.text, "error")

    def _complete(self, entry, answer):
        _, receipt = entry
        state = finish_message(receipt["message_id"], "completed", answer=answer)
        receipt.update(state=state, answer=answer)
        if state == "completed":
            self._deliver(entry)

    def _deliver(self, entry):
        event, receipt = entry
        if not self._current(entry):
            return
        ids = self._send(entry, receipt["answer"], "result")
        if (
            isinstance(ids, list)
            and ids
            and all(isinstance(mid, str) and mid.strip() for mid in ids)
        ):
            finish_message(receipt["message_id"], "delivered", delivery_ids=ids)
            self._react(event, "OK")


_dispatcher = MessageDispatcher()


def handle_incoming_message(event: IncomingMessage):
    """Authorization, command dispatch and conversation policy exist only here."""
    if not sender_allowed(event.sender_id):
        logger.warning(
            "Ignored unauthorized sender: %s chat=%s", event.sender_id, event.chat_id
        )
        return
    if not event.text.strip():
        return
    logger.info("[%s] %s", event.chat_type, event.text[:80])
    _dispatcher.submit(event)


def on_message(data):
    """SDK transport adapter; all business handling uses the normalized event."""
    try:
        msg, sender = data.event.message, data.event.sender
        payload = json.loads(msg.content or "{}")
        handle_incoming_message(
            _incoming(
                message_id=msg.message_id or "",
                chat_id=getattr(msg, "chat_id", "") or "",
                chat_type=msg.chat_type or "private",
                sender_id=sender.sender_id.open_id
                if sender and sender.sender_id
                else "",
                text=payload.get("text", ""),
            )
        )
    except Exception:
        logger.exception("Could not process Feishu SDK event")


def on_message_raw(message):
    """HTTP inbox transport adapter for the same normalized event handler."""
    try:
        handle_incoming_message(
            _incoming(
                **{
                    key: message.get(key, default)
                    for key, default in (
                        ("message_id", ""),
                        ("chat_id", ""),
                        ("chat_type", "private"),
                        ("sender_id", ""),
                        ("text", ""),
                    )
                }
            )
        )
    except Exception:
        logger.exception("Could not process Feishu inbox event")


def make_inbox_request():
    import urllib.request

    port = int(os.environ.get("ALPHAREPORT_PORT", "8000"))
    token = os.environ.get("FEISHU_INBOX_TOKEN", "")
    return urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/feishu/inbox",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )


def main():
    if not APP_ID or not APP_SECRET:
        logger.error("FEISHU_APP_ID/APP_SECRET not set")
        sys.exit(1)
    init_db()
    logger.info(f"Bot starting, project={PROJECT_ROOT}")
    from reportagent.services.local_pdf_import import import_pdf_library

    threading.Thread(
        target=lambda: asyncio.run(import_pdf_library()),
        name="local-pdf-import",
        daemon=True,
    ).start()

    # ── Webhook poller: reliable fallback when WebSocket drops ──
    import urllib.request as ur

    def poll_webhook():
        while True:
            try:
                req = make_inbox_request()
                with ur.urlopen(req, timeout=3) as resp:
                    msgs = json.loads(resp.read()).get("data", [])
                    for m in msgs:
                        logger.info(f"Webhook: {m.get('text', '')[:80]}")
                        on_message_raw(m)
            except Exception:
                pass
            time.sleep(1)

    if http_callback_enabled() and os.environ.get("FEISHU_INBOX_TOKEN"):
        threading.Thread(target=poll_webhook, daemon=True).start()

    # ── WebSocket: primary path ──
    eh = (
        dh.EventDispatcherHandlerBuilder(encrypt_key="", verification_token="")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    ws = WSClient(app_id=APP_ID, app_secret=APP_SECRET, event_handler=eh)

    while True:
        try:
            logger.info("Connecting to Feishu WebSocket...")
            ws.start()
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"WS: {e}")
        logger.info("Reconnecting in 5s...")
        time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    main()
