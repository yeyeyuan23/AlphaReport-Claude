"""A clear invalidates messages received before their chat worker is scheduled."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from reportagent.bot import claude, feishu, sessions


@pytest.fixture
def chat_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "SESSION_DB", tmp_path / "sessions.db")
    monkeypatch.setattr(claude, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(claude.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(claude, "build_system_prompt", lambda: "offline fixture")
    monkeypatch.setattr(feishu, "sender_allowed", lambda _: True)
    monkeypatch.setattr(feishu, "react", lambda *args: None)
    state = SimpleNamespace(threads=[], replies=[], model_calls=[])
    monkeypatch.setattr(
        feishu,
        "send_text",
        lambda recipient, text, **kwargs: state.replies.append(text)
        or ["om-delivered"],
    )

    class HeldThread:
        def __init__(self, target, args=(), **kwargs):
            if args:
                state.threads.append(lambda: target(*args))
            else:
                state.threads.append(target)

        def start(self):
            pass

    def model(cmd, **kwargs):
        state.model_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "MODEL ANSWER", "")

    monkeypatch.setattr(feishu.threading, "Thread", HeldThread)
    monkeypatch.setattr(claude.subprocess, "run", model)
    return state


@pytest.fixture(params=["websocket", "webhook"])
def dispatch(request, monkeypatch, chat_boundary):
    from itertools import count

    incoming_ids = count()
    if request.param == "websocket":

        def send(text):
            data = SimpleNamespace(
                event=SimpleNamespace(
                    message=SimpleNamespace(
                        message_id=f"offline-message-{next(incoming_ids)}",
                        chat_id="chat",
                        chat_type="p2p",
                        content=json.dumps({"text": text}),
                    ),
                    sender=SimpleNamespace(
                        sender_id=SimpleNamespace(open_id="offline-user")
                    ),
                )
            )
            feishu.on_message(data)

        return send

    # Exercise the nested webhook handler through one iteration of its real
    # inbox poller, without opening sockets or starting background threads.
    import urllib.request

    class PollFinished(BaseException):
        pass

    def stop_polling(*args):
        raise PollFinished

    def stop_websocket():
        raise KeyboardInterrupt

    monkeypatch.setattr(feishu, "APP_ID", "offline-app")
    monkeypatch.setattr(feishu, "APP_SECRET", "offline-secret")
    monkeypatch.setattr(feishu, "init_db", lambda: None)
    monkeypatch.setattr(feishu, "http_callback_enabled", lambda: True)
    monkeypatch.setenv("FEISHU_INBOX_TOKEN", "offline-token")
    monkeypatch.setattr(
        feishu, "WSClient", lambda **kwargs: SimpleNamespace(start=stop_websocket)
    )
    monkeypatch.setattr(feishu.time, "sleep", stop_polling)
    feishu.main()
    poller = next(
        target for target in chat_boundary.threads if target.__name__ == "poll_webhook"
    )
    chat_boundary.threads.clear()

    class InboxResponse:
        def __init__(self, text):
            self.text = text

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(
                {
                    "data": [
                        {
                            "message_id": f"offline-message-{next(incoming_ids)}",
                            "chat_id": "chat",
                            "chat_type": "p2p",
                            "sender_id": "offline-user",
                            "text": self.text,
                        }
                    ]
                }
            ).encode()

    def send(text):
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda *args, **kwargs: InboxResponse(text)
        )
        with pytest.raises(PollFinished):
            poller()

    return send


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("replacement", [False, True])
def test_clear_before_worker_starts_invalidates_received_message(
    dispatch,
    chat_boundary,
    existing,
    replacement,
):
    if existing:
        sessions.save_session("chat", "old-session", "old-summary")
    dispatch("question received before clear")
    assert len(chat_boundary.threads) == 1
    worker = chat_boundary.threads.pop()
    dispatch("/clear")
    assert sessions.get_session("chat") == (None, "")
    if replacement:
        sessions.save_session("chat", "replacement-session", "replacement-summary")

    worker()

    expected = (
        ("replacement-session", "replacement-summary") if replacement else (None, "")
    )
    assert sessions.get_session("chat") == expected
    assert chat_boundary.model_calls == []
    assert "MODEL ANSWER" not in chat_boundary.replies


def test_current_received_message_still_runs(dispatch, chat_boundary):
    dispatch("current question")
    assert len(chat_boundary.threads) == 1
    chat_boundary.threads.pop()()
    assert chat_boundary.replies[-1] == "MODEL ANSWER"
    sid, summary = sessions.get_session("chat")
    assert sid is not None
    assert summary == ""


def test_two_argument_ask_claude_remains_compatible(chat_boundary):
    assert claude.ask_claude("direct question", "chat") == "MODEL ANSWER"
    assert sessions.get_session("chat")[0] is not None
