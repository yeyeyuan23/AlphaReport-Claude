"""SDK and inbox transports must share authorization, session and reply policy."""

from types import SimpleNamespace

import pytest

from reportagent.bot import feishu as bot


@pytest.mark.parametrize("kind", ["private", "group"])
@pytest.mark.parametrize("command", [True, False])
def test_ws_and_http_use_the_same_message_handler(monkeypatch, kind, command):
    observed = []
    monkeypatch.setattr(
        bot,
        "handle_incoming_message",
        lambda event: observed.append(event),
        raising=False,
    )
    sdk = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="m",
                chat_id="oc_chat",
                chat_type=kind,
                content='{"text":"/stats"}' if command else '{"text":"question"}',
            ),
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_user")),
        )
    )
    bot.on_message(sdk)
    bot.on_message_raw(
        {
            "message_id": "m",
            "chat_id": "oc_chat",
            "chat_type": kind,
            "sender_id": "ou_user",
            "text": "/stats" if command else "question",
        }
    )
    assert len(observed) == 2
    assert observed[0] == observed[1]


def test_common_handler_captures_generation_before_any_acknowledgement(monkeypatch):
    from reportagent.bot import sessions

    events = []
    generation = sessions.get_reset_generation("oc_chat")
    monkeypatch.setattr(bot, "sender_allowed", lambda _: True)

    def react(*args):
        events.append("react")
        sessions.del_session("oc_chat")

    monkeypatch.setattr(bot, "react", react)
    monkeypatch.setattr(bot, "send_text", lambda *args, **kwargs: ["om-ack"])

    def forbidden(*args, **kwargs):
        raise AssertionError("Cancelled generation reached the model")

    monkeypatch.setattr(bot, "ask_claude_outcome", forbidden)

    class Thread:
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(bot.threading, "Thread", Thread)
    bot.on_message_raw(
        {
            "message_id": "m",
            "chat_id": "oc_chat",
            "chat_type": "private",
            "sender_id": "ou_user",
            "text": "question",
        }
    )
    assert events == ["react"]
    assert sessions.get_reset_generation("oc_chat") == generation + 1


@pytest.mark.parametrize("text,allowed", [("", True), ("question", False)])
def test_common_handler_drops_empty_or_unauthorized_events(monkeypatch, text, allowed):
    monkeypatch.setattr(bot, "sender_allowed", lambda _: allowed)

    def forbidden(*args, **kwargs):
        raise AssertionError("Rejected event reached session or model")

    monkeypatch.setattr(bot, "reserve_message", forbidden)
    monkeypatch.setattr(bot, "ask_claude_outcome", forbidden)
    bot.on_message_raw(
        {
            "message_id": "m",
            "chat_id": "oc_chat",
            "chat_type": "private",
            "sender_id": "ou_user",
            "text": text,
        }
    )


@pytest.mark.parametrize("transport", ["ws", "http"])
@pytest.mark.parametrize("command", ["/daily", "/weekly", "/report 2005.13665"])
@pytest.mark.parametrize("chat_type", ["p2p", "group"])
def test_real_report_commands_keep_private_chat_authorization(
    monkeypatch, transport, command, chat_type
):
    from threading import Lock

    replies, jobs = [], []
    monkeypatch.setattr(bot, "sender_allowed", lambda _: True)
    monkeypatch.setattr(bot, "react", lambda *args: None)
    monkeypatch.setattr(
        bot,
        "send_text",
        lambda _, text, **kwargs: replies.append(text) or ["om-delivered"],
    )
    monkeypatch.setattr(
        bot,
        "reply_msg",
        lambda _, text, **kwargs: replies.append(text) or ["om-delivered"],
    )
    monkeypatch.setattr(bot, "_report_lock", Lock())

    class Thread:
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args

        def start(self):
            jobs.append((self.target, self.args))

    monkeypatch.setattr(bot.threading, "Thread", Thread)
    message = {
        "message_id": "m",
        "chat_id": "oc_chat",
        "chat_type": chat_type,
        "sender_id": "ou_user",
        "text": command,
    }
    if transport == "http":
        bot.on_message_raw(message)
    else:
        import json

        bot.on_message(
            SimpleNamespace(
                event=SimpleNamespace(
                    message=SimpleNamespace(
                        message_id="m",
                        chat_id="oc_chat",
                        chat_type=chat_type,
                        content=json.dumps({"text": command}),
                    ),
                    sender=SimpleNamespace(
                        sender_id=SimpleNamespace(open_id="ou_user")
                    ),
                )
            )
        )
    assert len(replies) == 1
    if chat_type == "p2p":
        assert len(jobs) == 1
        assert "开始" in replies[0]
    else:
        assert jobs == []
        assert "私聊" in replies[0]
