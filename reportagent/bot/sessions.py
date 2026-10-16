"""Persistent chat-to-Claude session mapping."""
import sqlite3
from contextlib import closing
from reportagent.utils.config import PROJECT_ROOT

SESSION_DB = PROJECT_ROOT / "data" / "sessions.db"

def _connect():
    SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SESSION_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (chat_id TEXT PRIMARY KEY, session_id TEXT, summary TEXT)")
    conn.commit()
    return conn

def get_session(chat_id):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT session_id, summary FROM sessions WHERE chat_id=?", (chat_id,)).fetchone()
    return (row[0], row[1]) if row else (None, "")

def save_session(chat_id, sid, summary=""):
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?)", (chat_id, sid, summary or ""))

def del_session(chat_id):
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
