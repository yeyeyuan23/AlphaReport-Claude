"""Real-thread, offline ingress regressions for ordering and replay handling."""

import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from reportagent.bot import claude, feishu as bot, sessions


@pytest.fixture
def chat(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "SESSION_DB", tmp_path / "sessions.db")
    monkeypatch.setattr(claude, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(claude.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(claude, "build_system_prompt", lambda: "offline fixture")
    monkeypatch.setattr(bot, "sender_allowed", lambda _: True)
    monkeypatch.setattr(bot, "react", lambda *args: None)
    if hasattr(bot, "MessageDispatcher"):
        monkeypatch.setattr(bot, "_dispatcher", bot.MessageDispatcher())
    state = SimpleNamespace(calls=[], replies=[], threads=[])
    real_thread = threading.Thread

    class TrackedThread(real_thread):
        def start(self):
            state.threads.append(self)
            super().start()

    monkeypatch.setattr(bot.threading, "Thread", TrackedThread)

    def send(recipient, text, **kwargs):
        state.replies.append((recipient, text, kwargs.get("request_id")))
        return ["om-delivered"]

    def model(cmd, **kwargs):
        state.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "ANSWER " + cmd[2], "")

    monkeypatch.setattr(bot, "send_text", send)
    monkeypatch.setattr(bot, "reply_msg", send)
    monkeypatch.setattr(bot, "send_chat", send)
    monkeypatch.setattr(claude.subprocess, "run", model)
    state.send, state.model = send, model

    def wait():
        for thread in state.threads:
            if thread.ident is not None:
                thread.join(5)
                assert not thread.is_alive(), "dispatcher worker did not finish"

    state.wait = wait
    yield state
    wait()


def deliver(
    message_id,
    text="question",
    *,
    transport="sdk",
    chat_id="chat",
    sender="user",
    kind="p2p",
):
    message = dict(
        message_id=message_id,
        text=text,
        chat_id=chat_id,
        sender_id=sender,
        chat_type=kind,
    )
    if transport == "http":
        bot.on_message_raw(message)
    else:
        bot.on_message(
            SimpleNamespace(
                event=SimpleNamespace(
                    message=SimpleNamespace(
                        message_id=message_id,
                        chat_id=chat_id,
                        chat_type=kind,
                        content=json.dumps({"text": text}),
                    ),
                    sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=sender)),
                )
            )
        )


def answers(chat):
    return [text for _, text, _ in chat.replies if text.startswith("ANSWER")]


def test_same_chat_sdk_turns_wait_and_resume_the_preceding_session(chat, monkeypatch):
    started, release, second_started = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def model(cmd, **kwargs):
        if cmd[2] == "A":
            started.set()
            assert release.wait(5)
        else:
            second_started.set()
        return chat.model(cmd, **kwargs)

    monkeypatch.setattr(claude.subprocess, "run", model)
    deliver("a", "A")
    try:
        assert started.wait(5)
        deliver("b", "B")
        assert not second_started.wait(0.15), "B ran concurrently with A"
    finally:
        release.set()
        chat.wait()
    assert answers(chat) == ["ANSWER A", "ANSWER B"]
    first, second = chat.calls
    assert (
        second[second.index("--resume") + 1] == first[first.index("--session-id") + 1]
    )


def test_clear_starts_new_generation_without_waiting_for_old_worker(chat, monkeypatch):
    started, release, fresh = threading.Event(), threading.Event(), threading.Event()

    def model(cmd, **kwargs):
        if cmd[2] == "A":
            started.set()
            assert release.wait(5)
        if cmd[2] == "C":
            fresh.set()
        return chat.model(cmd, **kwargs)

    monkeypatch.setattr(claude.subprocess, "run", model)
    deliver("a", "A")
    try:
        assert started.wait(5)
        deliver("b", "B")
        deliver("clear", "<at user_id='bot'>bot</at> /clear")
        deliver("c", "C")
        assert fresh.wait(5), "new generation was blocked by cancelled work"
    finally:
        release.set()
        chat.wait()
    assert [cmd[2] for cmd in chat.calls].count("B") == 0
    assert answers(chat) == ["ANSWER C"]


def test_different_chats_can_run_concurrently(chat, monkeypatch):
    started, release, other = threading.Event(), threading.Event(), threading.Event()

    def model(cmd, **kwargs):
        if cmd[2] == "A":
            started.set()
            assert release.wait(5)
        else:
            other.set()
        return chat.model(cmd, **kwargs)

    monkeypatch.setattr(claude.subprocess, "run", model)
    deliver("a", "A")
    try:
        assert started.wait(5)
        deliver("b", "B", chat_id="other")
        assert other.wait(5)
    finally:
        release.set()
        chat.wait()
    assert sorted(answers(chat)) == ["ANSWER A", "ANSWER B"]


def test_running_and_queued_duplicates_are_not_executed(chat, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def model(cmd, **kwargs):
        if cmd[2] == "A":
            started.set()
            assert release.wait(5)
        return chat.model(cmd, **kwargs)

    monkeypatch.setattr(claude.subprocess, "run", model)
    deliver("a", "A")
    try:
        assert started.wait(5)
        deliver("a", "A")
        deliver("b", "B")
        deliver("b", "B", transport="http")
    finally:
        release.set()
        chat.wait()
    assert [cmd[2] for cmd in chat.calls] == ["A", "B"]
    assert answers(chat) == ["ANSWER A", "ANSWER B"]


@pytest.mark.parametrize("transport", ["sdk", "http"])
def test_delivered_same_id_replay_does_not_repeat_answer(chat, transport):
    deliver("same")
    chat.wait()
    deliver("same", transport=transport)
    chat.wait()
    assert len(chat.calls) == 1
    assert answers(chat) == ["ANSWER question"]


@pytest.mark.parametrize("failure", ["empty", "invalid", "exception"])
def test_delivery_failure_retries_cached_answer_without_model(
    chat, monkeypatch, failure
):
    def fail(recipient, text, **kwargs):
        chat.send(recipient, text, **kwargs)
        if text.startswith("ANSWER"):
            if failure == "exception":
                raise RuntimeError("offline send failure")
            return [] if failure == "empty" else [""]
        return ["om-ack"]

    monkeypatch.setattr(bot, "send_text", fail)
    deliver("same")
    chat.wait()
    monkeypatch.setattr(bot, "send_text", chat.send)
    deliver("same")
    chat.wait()
    assert len(chat.calls) == 1
    assert answers(chat) == ["ANSWER question", "ANSWER question"]
    answer_ids = [
        request_id for _, text, request_id in chat.replies if text.startswith("ANSWER")
    ]
    assert answer_ids[0] and answer_ids[0] == answer_ids[1]
    deliver("same")
    chat.wait()
    assert len(answers(chat)) == 2


def test_failed_model_can_retry_same_incoming_id(chat, monkeypatch):
    def fail(cmd, **kwargs):
        chat.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "temporary model failure")

    monkeypatch.setattr(claude.subprocess, "run", fail)
    deliver("same")
    chat.wait()
    monkeypatch.setattr(claude.subprocess, "run", chat.model)
    deliver("same")
    chat.wait()
    assert len(chat.calls) == 2
    assert answers(chat) == ["ANSWER question"]


@pytest.mark.parametrize("prefix", ["失败:", "出错:", "Claude Code 未安装"])
def test_successful_answer_failure_words_do_not_make_work_retryable(
    chat, monkeypatch, prefix
):
    def model(cmd, **kwargs):
        chat.calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, prefix + " is the quoted message", ""
        )

    monkeypatch.setattr(claude.subprocess, "run", model)
    deliver("same")
    chat.wait()
    deliver("same")
    chat.wait()
    assert len(chat.calls) == 1
    assert sum(text.startswith(prefix) for _, text, _ in chat.replies) == 1


def test_replayed_clear_cannot_clear_newer_session(chat):
    deliver("clear", "/clear")
    deliver("question")
    chat.wait()
    saved = sessions.get_session("chat")
    deliver("clear", "/clear", transport="http")
    chat.wait()
    assert sessions.get_session("chat") == saved


@pytest.mark.parametrize(
    "change",
    [
        {"sender": "other"},
        {"chat_id": "other"},
        {"kind": "group"},
        {"text": "different"},
    ],
)
def test_same_id_with_changed_route_or_payload_is_rejected(chat, change):
    deliver("same")
    chat.wait()
    before = list(chat.replies)
    deliver("same", **change)
    chat.wait()
    assert len(chat.calls) == 1
    assert chat.replies == before


def test_reaction_and_ack_errors_do_not_prevent_question_or_next_turn(
    chat, monkeypatch
):
    def react(*args):
        raise RuntimeError("offline reaction failure")

    def send(recipient, text, **kwargs):
        if text.startswith("🤔"):
            raise RuntimeError("offline ack failure")
        return chat.send(recipient, text, **kwargs)

    monkeypatch.setattr(bot, "react", react)
    monkeypatch.setattr(bot, "send_text", send)
    deliver("a", "A")
    deliver("b", "B")
    chat.wait()
    assert answers(chat) == ["ANSWER A", "ANSWER B"]


def test_completion_survives_dispatcher_recreation(chat, monkeypatch):
    monkeypatch.setattr(bot, "send_text", lambda *args, **kwargs: [])
    deliver("same")
    chat.wait()
    monkeypatch.setattr(bot, "_dispatcher", bot.MessageDispatcher())
    monkeypatch.setattr(bot, "send_text", chat.send)
    deliver("same")
    chat.wait()
    assert len(chat.calls) == 1
    assert answers(chat) == ["ANSWER question"]


def test_old_cached_answer_is_cancelled_after_clear(chat, monkeypatch):
    monkeypatch.setattr(bot, "send_text", lambda *args, **kwargs: [])
    deliver("old")
    chat.wait()
    deliver("clear", "/clear")
    monkeypatch.setattr(bot, "send_text", chat.send)
    deliver("old")
    chat.wait()
    assert len(chat.calls) == 1
    assert chat.replies == []


@pytest.mark.parametrize(
    "command,job",
    [
        ("/report 2005.13665", "run_report_job"),
        ("/daily", "run_daily_job"),
        ("/weekly", "run_weekly_job"),
    ],
)
def test_accepted_job_dispatch_is_not_repeated_when_ack_fails(
    chat, monkeypatch, command, job
):
    runs = []
    monkeypatch.setattr(bot, "_report_lock", threading.Lock())

    def execute(*args):
        runs.append(args)
        bot._report_lock.release()

    monkeypatch.setattr(bot, job, execute)
    monkeypatch.setattr(bot, "send_text", lambda *args, **kwargs: [])
    deliver("job", command)
    chat.wait()
    monkeypatch.setattr(bot, "send_text", chat.send)
    deliver("job", command)
    chat.wait()
    assert len(runs) == 1
    assert len(chat.replies) == 1 and "开始" in chat.replies[0][1]
    assert chat.calls == []


def test_start_failure_releases_claim_for_retry_and_next_message(chat, monkeypatch):
    thread = bot.threading.Thread

    class BrokenThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(bot.threading, "Thread", BrokenThread)
    deliver("same")
    monkeypatch.setattr(bot.threading, "Thread", thread)
    deliver("same")
    deliver("next", "next")
    chat.wait()
    assert answers(chat) == ["ANSWER question", "ANSWER next"]


def test_reset_generation_survives_empty_clear_but_not_normal_save(chat):
    before = sessions.get_reset_generation("chat")
    sessions.save_session("chat", "first")
    assert sessions.get_reset_generation("chat") == before
    sessions.del_session("chat")
    sessions.del_session("chat")
    assert sessions.get_reset_generation("chat") == before + 2


def test_missing_session_recovery_does_not_cancel_queued_turn(chat, monkeypatch):
    sessions.save_session("chat", "missing-session")
    started, release = threading.Event(), threading.Event()

    def model(cmd, **kwargs):
        if "--resume" in cmd and "missing-session" in cmd:
            started.set()
            assert release.wait(5)
            chat.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "Session not found")
        return chat.model(cmd, **kwargs)

    monkeypatch.setattr(claude.subprocess, "run", model)
    deliver("a", "A")
    try:
        assert started.wait(5)
        deliver("b", "B")
    finally:
        release.set()
        chat.wait()
    assert answers(chat) == ["ANSWER A", "ANSWER B"]
    assert "--resume" in chat.calls[-1]


def test_worker_exception_does_not_wedge_tail_or_retry(chat, monkeypatch):
    original = claude.build_system_prompt
    started, release = threading.Event(), threading.Event()
    attempts = []

    def build():
        attempts.append(True)
        if len(attempts) == 1:
            started.set()
            assert release.wait(5)
            raise RuntimeError("unexpected worker failure")
        return original()

    monkeypatch.setattr(claude, "build_system_prompt", build)
    deliver("a", "A")
    try:
        assert started.wait(5)
        deliver("b", "B")
    finally:
        release.set()
        chat.wait()
    deliver("a", "A")
    chat.wait()
    assert answers(chat) == ["ANSWER B", "ANSWER A"]
    assert any("处理失败" in text for _, text, _ in chat.replies)


@pytest.mark.parametrize("kind", ["p2p", "group"])
def test_clear_between_chunks_cancels_unsent_answer_parts(chat, monkeypatch, kind):
    from reportagent.bot import client

    sent = []

    def api(request):
        text = json.loads(request.request_body.content)["text"]
        sent.append(text)
        if text.startswith("ANSWER"):
            deliver("clear", "/clear")
        return SimpleNamespace(
            success=lambda: True, data=SimpleNamespace(message_id="om-sent")
        )

    sdk = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(message=SimpleNamespace(create=api, reply=api))
        )
    )
    monkeypatch.setattr(client, "_client", lambda: sdk)
    monkeypatch.setattr(bot, "send_text", client.send_text)
    monkeypatch.setattr(bot, "reply_msg", client.reply_msg)
    monkeypatch.setattr(
        claude.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 0, "ANSWER" + "x" * 15000, ""
        ),
    )
    deliver("long", kind=kind)
    chat.wait()
    assert not any(text.startswith("x") for text in sent)
    assert sessions.get_session("chat") == (None, "")


@pytest.mark.parametrize("kind", ["p2p", "group"])
def test_partial_pipeline_delivery_retries_each_chunk_with_stable_uuid(
    chat, monkeypatch, kind
):
    from reportagent.bot import client

    requests = []
    failed = False

    def api(request):
        nonlocal failed
        text = json.loads(request.request_body.content)["text"]
        if not text.startswith("🤔"):
            requests.append((text, request.request_body.uuid))
            if len(requests) == 2 and not failed:
                failed = True
                return SimpleNamespace(success=lambda: False, code=500)
        return SimpleNamespace(
            success=lambda: True, data=SimpleNamespace(message_id="om-sent")
        )

    sdk = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(message=SimpleNamespace(create=api, reply=api))
        )
    )
    monkeypatch.setattr(client, "_client", lambda: sdk)
    monkeypatch.setattr(bot, "send_text", client.send_text)
    monkeypatch.setattr(bot, "reply_msg", client.reply_msg)

    def model(cmd, **kwargs):
        chat.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "ANSWER" + "x" * 15000, "")

    monkeypatch.setattr(claude.subprocess, "run", model)
    deliver("long", kind=kind)
    chat.wait()
    deliver("long", kind=kind)
    chat.wait()
    assert len(chat.calls) == 1
    assert len(requests) == 5
    assert requests[:2] == requests[2:4]
    assert all(uid for _, uid in requests)
    assert len({uid for _, uid in requests}) == 3


@pytest.mark.parametrize(
    "text",
    [
        "/daily extra question",
        "/stats extra",
        "/help extra",
        "/search\ntext",
        "/report\n2005.13665",
    ],
)
def test_unknown_command_syntax_enters_model_fifo_at_admission(chat, monkeypatch, text):
    def forbidden(*args):
        raise AssertionError("Ordinary model text was dispatched outside its FIFO")

    monkeypatch.setattr(bot, "handle_cmd", forbidden)
    deliver("question", text)
    deliver("next", "next")
    chat.wait()
    assert answers(chat) == ["ANSWER " + text, "ANSWER next"]


def test_upgrade_keeps_legacy_revision_mapping_and_summary(chat):
    import sqlite3

    with sqlite3.connect(sessions.SESSION_DB) as conn:
        conn.execute(
            "CREATE TABLE sessions (chat_id TEXT PRIMARY KEY, session_id TEXT, summary TEXT)"
        )
        conn.execute(
            "CREATE TABLE session_revisions (chat_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO sessions VALUES ('chat','legacy','summary')")
        conn.execute("INSERT INTO session_revisions VALUES ('chat',11)")
    assert sessions.get_session_snapshot("chat") == ("legacy", "summary", 11)
    assert sessions.get_reset_generation("chat") == 0
    sessions.save_session("chat", "updated", expected_revision=11)
    assert sessions.get_reset_generation("chat") == 0
    sessions.del_session("chat")
    assert sessions.get_session_snapshot("chat") == (None, "", 13)
    assert sessions.get_reset_generation("chat") == 1
