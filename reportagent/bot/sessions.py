"""Persistent chat sessions with revisions that survive clearing the mapping."""
import sqlite3
from contextlib import closing
from reportagent.utils.config import PROJECT_ROOT

SESSION_DB = PROJECT_ROOT / "data" / "sessions.db"


def _connect():
    SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SESSION_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (chat_id TEXT PRIMARY KEY, session_id TEXT, summary TEXT)")
    # Keep revision tombstones separately so existing databases need no column migration.
    conn.execute("CREATE TABLE IF NOT EXISTS session_revisions (chat_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
    conn.commit()
    return conn


def get_session_snapshot(chat_id):
    """Read the mapping and its revision in one transaction, including empty chats."""
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO session_revisions VALUES (?, 0)", (chat_id,))
        row = conn.execute(
            "SELECT s.session_id, s.summary, r.revision FROM session_revisions r "
            "LEFT JOIN sessions s ON s.chat_id=r.chat_id WHERE r.chat_id=?", (chat_id,)
        ).fetchone()
    return row[0], row[1] or "", row[2]


def get_session(chat_id):
    sid, summary, _ = get_session_snapshot(chat_id)
    return sid, summary


def _advance_revision(conn, chat_id, expected_revision):
    conn.execute("INSERT OR IGNORE INTO session_revisions VALUES (?, 0)", (chat_id,))
    if expected_revision is None:
        conn.execute("UPDATE session_revisions SET revision=revision+1 WHERE chat_id=?", (chat_id,))
    else:
        changed = conn.execute(
            "UPDATE session_revisions SET revision=revision+1 WHERE chat_id=? AND revision=?",
            (chat_id, expected_revision),
        )
        if changed.rowcount != 1:
            return None
    return conn.execute("SELECT revision FROM session_revisions WHERE chat_id=?", (chat_id,)).fetchone()[0]


def save_session(chat_id, sid, summary="", *, expected_revision=None):
    """Save only if the caller's snapshot is current; return the new revision."""
    with closing(_connect()) as conn, conn:
        revision = _advance_revision(conn, chat_id, expected_revision)
        if revision is not None:
            conn.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?)", (chat_id, sid, summary or ""))
    return revision


def del_session(chat_id, *, expected_revision=None):
    """Invalidate even absent sessions so an older in-flight request cannot restore them."""
    with closing(_connect()) as conn, conn:
        revision = _advance_revision(conn, chat_id, expected_revision)
        if revision is not None:
            conn.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
    return revision
