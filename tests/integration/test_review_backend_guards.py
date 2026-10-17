"""Offline regressions for selector, session-clear and PDF response boundaries."""

import asyncio
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.bot import claude, sessions
from reportagent.cli import classify as classify_cli
from reportagent.services import batch_classification as classification
from reportagent.models.database import Base, Report


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"report_id": 0},
        {"report_id": -1},
        {"recent": 0},
        {"recent": -1},
        {"all": True, "report_id": 1},
        {"all": True, "recent": 1},
        {"report_id": 1, "recent": 1},
        {"report_id": True},
        {"recent": 1.5},
        {"all": "yes"},
    ],
)
def test_invalid_classification_selection_fails_before_db_or_model(monkeypatch, values):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid selector reached database or model initialization")

    monkeypatch.setattr(classification, "init_db", forbidden)
    monkeypatch.setattr(classification, "LLMClient", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(
            classification.classify_async(
                classification.ClassificationOptions(llm=True, **values)
            )
        )


@pytest.mark.parametrize(
    "args",
    [
        ["--report-id", "0"],
        ["--report-id", "-1"],
        ["--recent", "0"],
        ["--recent", "-1"],
        ["--all", "--recent", "1"],
        [],
    ],
)
def test_cli_rejects_invalid_selectors_before_service(monkeypatch, args):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid CLI selector reached service")

    monkeypatch.setattr(classification, "classify_async", forbidden)
    monkeypatch.setattr(sys, "argv", ["classify", *args])
    with pytest.raises(SystemExit) as error:
        classify_cli.main()
    assert error.value.code == 2


@pytest.fixture
def isolated_chat(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "SESSION_DB", tmp_path / "sessions.db")
    monkeypatch.setattr(claude, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(claude.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(claude, "build_system_prompt", lambda: "offline fixture")
    return tmp_path


@pytest.mark.parametrize("existing", [False, True])
def test_clear_during_running_chat_cannot_restore_session(
    monkeypatch, isolated_chat, existing
):
    if existing:
        sessions.save_session("chat", "old-session", "old-summary")
    started, finish = Event(), Event()

    def run(cmd, **kwargs):
        started.set()
        assert finish.wait(5), "test did not release the simulated model"
        return subprocess.CompletedProcess(cmd, 0, "answer", "")

    monkeypatch.setattr(claude.subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(claude.ask_claude, "question", "chat")
        try:
            assert started.wait(5)
            sessions.del_session("chat")
        finally:
            finish.set()
        pending.result(timeout=5)
    assert sessions.get_session("chat") == (None, "")


def test_clear_while_compacting_does_not_save_summary_or_restart(
    monkeypatch, isolated_chat
):
    sessions.save_session("chat", "large-session", "old-summary")
    directory = (
        isolated_chat
        / ".claude-bot"
        / "projects"
        / str(isolated_chat).replace("/", "-").lstrip("-")
    )
    directory.mkdir(parents=True)
    (directory / "large-session.jsonl").write_text("x" * (51 * 1024))

    def compact(*args):
        sessions.del_session("chat")
        return "stale-summary"

    monkeypatch.setattr(claude, "_compact_session", compact)
    monkeypatch.setattr(
        claude.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "answer", ""),
    )
    claude.ask_claude("question", "chat")
    assert sessions.get_session("chat") == (None, "")


def test_old_resume_error_cannot_delete_newer_session_or_retry(
    monkeypatch, isolated_chat
):
    sessions.save_session("chat", "old-session", "old-summary")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            sessions.del_session("chat")
            sessions.save_session("chat", "new-session", "new-summary")
            return subprocess.CompletedProcess(cmd, 1, "", "Session not found")
        return subprocess.CompletedProcess(cmd, 0, "answer", "")

    monkeypatch.setattr(claude.subprocess, "run", run)
    claude.ask_claude("question", "chat")
    assert sessions.get_session("chat") == ("new-session", "new-summary")
    assert len(calls) == 1


def test_first_remote_pdf_and_cached_pdf_have_identical_inline_headers(
    monkeypatch, tmp_path
):
    from reportagent.api import library

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(library, "PROJECT_ROOT", tmp_path)
    payload = b"%PDF-1.4\nfixture"
    async_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload)
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=transport, **kwargs),
    )
    try:
        with factory() as db:
            report = Report(
                title="paper",
                source="arxiv",
                source_url="https://arxiv.org/abs/1234.56789",
                arxiv_id="1234.56789",
                content_hash="fixture",
            )
            db.add(report)
            db.commit()
            first = library.get_report_pdf(report.id, db)
            cached = library.get_report_pdf(report.id, db)
            expected = f'inline; filename="report_{report.id}.pdf"'
            assert first.headers["content-disposition"] == expected
            assert cached.headers["content-disposition"] == expected
            assert (
                first.headers["content-type"]
                == cached.headers["content-type"]
                == "application/pdf"
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "options, expected_ids",
    [
        (classification.ClassificationOptions(report_id=2), [2]),
        (classification.ClassificationOptions(recent=1), [3]),
        (classification.ClassificationOptions(all=True), [1, 2, 3]),
    ],
)
def test_valid_selector_classifies_only_its_explicit_scope(
    monkeypatch, options, expected_ids
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(classification, "init_db", lambda: None)
    monkeypatch.setattr(classification, "get_session_factory", lambda: factory)
    try:
        with factory() as db:
            db.add_all(
                [
                    Report(
                        title=f"Factor model {i}", content_hash=str(i), source="arxiv"
                    )
                    for i in range(3)
                ]
            )
            db.commit()
        audit = asyncio.run(classification.classify_async(options))
        assert audit["report_ids"] == expected_ids
        assert audit["classified_ids"] == expected_ids
        assert audit["selected"] == len(expected_ids)
    finally:
        engine.dispose()


def test_old_snapshot_cannot_save_after_repeated_clear_and_reopen(isolated_chat):
    # No mapping -> clear -> replacement -> clear produces the same visible
    # mapping, but must never make the original request valid again (ABA).
    _, _, revision = sessions.get_session_snapshot("chat")
    sessions.del_session("chat")
    sessions.save_session("chat", "intermediate")
    sessions.del_session("chat")
    assert sessions.save_session("chat", "stale", expected_revision=revision) is None
    assert sessions.get_session("chat") == (None, "")


def test_existing_session_database_is_read_without_losing_saved_summary(isolated_chat):
    import sqlite3

    with sqlite3.connect(sessions.SESSION_DB) as db:
        db.execute(
            "CREATE TABLE sessions (chat_id TEXT PRIMARY KEY, session_id TEXT, summary TEXT)"
        )
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)",
            ("chat", "legacy-session", "legacy-summary"),
        )
    assert sessions.get_session("chat") == ("legacy-session", "legacy-summary")
    _, _, revision = sessions.get_session_snapshot("chat")
    sessions.del_session("chat")
    assert sessions.save_session("chat", "stale", expected_revision=revision) is None
    assert sessions.get_session("chat") == (None, "")


@pytest.mark.parametrize("compact", [False, True])
def test_clear_during_post_recovery_or_compact_call_stays_cleared(
    monkeypatch, isolated_chat, compact
):
    sessions.save_session("chat", "old-session", "old-summary")
    if compact:
        directory = (
            isolated_chat
            / ".claude-bot"
            / "projects"
            / str(isolated_chat).replace("/", "-").lstrip("-")
        )
        directory.mkdir(parents=True)
        (directory / "old-session.jsonl").write_text("x" * (51 * 1024))
        monkeypatch.setattr(claude, "_compact_session", lambda *args: "new-summary")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if not compact and len(calls) == 1:
            return subprocess.CompletedProcess(cmd, 1, "", "Session not found")
        assert "--session-id" in cmd
        sessions.del_session("chat")
        return subprocess.CompletedProcess(cmd, 0, "answer", "")

    monkeypatch.setattr(claude.subprocess, "run", run)
    claude.ask_claude("question", "chat")
    assert sessions.get_session("chat") == (None, "")
    assert len(calls) == (1 if compact else 2)
