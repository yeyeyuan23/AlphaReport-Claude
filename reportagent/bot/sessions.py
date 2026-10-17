"""Persistent chat sessions with revisions that survive clearing the mapping."""

import hashlib
import json
import sqlite3
from contextlib import closing
from reportagent.utils.config import PROJECT_ROOT

SESSION_DB = PROJECT_ROOT / "data" / "sessions.db"


def _connect():
    SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SESSION_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions (chat_id TEXT PRIMARY KEY, session_id TEXT, summary TEXT)"
    )
    # Keep revision tombstones separately so existing databases need no column migration.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_revisions (chat_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_generations "
        "(chat_id TEXT PRIMARY KEY, generation INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS message_receipts ("
        "message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, chat_type TEXT NOT NULL, "
        "sender_id TEXT NOT NULL, payload_hash TEXT NOT NULL, generation INTEGER NOT NULL, "
        "state TEXT NOT NULL, answer TEXT, delivery_ids TEXT NOT NULL DEFAULT '[]')"
    )
    conn.commit()
    return conn


def get_session_snapshot(chat_id):
    """Read the mapping and its revision in one transaction, including empty chats."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO session_revisions VALUES (?, 0)", (chat_id,)
        )
        row = conn.execute(
            "SELECT s.session_id, s.summary, r.revision FROM session_revisions r "
            "LEFT JOIN sessions s ON s.chat_id=r.chat_id WHERE r.chat_id=?",
            (chat_id,),
        ).fetchone()
    return row[0], row[1] or "", row[2]


def get_session(chat_id):
    sid, summary, _ = get_session_snapshot(chat_id)
    return sid, summary


def _advance_revision(conn, chat_id, expected_revision):
    conn.execute("INSERT OR IGNORE INTO session_revisions VALUES (?, 0)", (chat_id,))
    if expected_revision is None:
        conn.execute(
            "UPDATE session_revisions SET revision=revision+1 WHERE chat_id=?",
            (chat_id,),
        )
    else:
        changed = conn.execute(
            "UPDATE session_revisions SET revision=revision+1 WHERE chat_id=? AND revision=?",
            (chat_id, expected_revision),
        )
        if changed.rowcount != 1:
            return None
    return conn.execute(
        "SELECT revision FROM session_revisions WHERE chat_id=?", (chat_id,)
    ).fetchone()[0]


def _generation(conn, chat_id):
    conn.execute("INSERT OR IGNORE INTO session_generations VALUES (?, 0)", (chat_id,))
    return conn.execute(
        "SELECT generation FROM session_generations WHERE chat_id=?", (chat_id,)
    ).fetchone()[0]


def get_reset_generation(chat_id):
    """Only an explicit reset advances this durable cancellation boundary."""
    with closing(_connect()) as conn, conn:
        return _generation(conn, chat_id)


def save_session(
    chat_id, sid, summary="", *, expected_revision=None, expected_generation=None
):
    """Save only if the caller's snapshot is current; return the new revision."""
    with closing(_connect()) as conn, conn:
        if (
            expected_generation is not None
            and _generation(conn, chat_id) != expected_generation
        ):
            return None
        revision = _advance_revision(conn, chat_id, expected_revision)
        if revision is not None:
            conn.execute(
                "INSERT OR REPLACE INTO sessions VALUES(?,?,?)",
                (chat_id, sid, summary or ""),
            )
    return revision


def del_session(
    chat_id, *, expected_revision=None, expected_generation=None, reset=True
):
    """Invalidate even absent sessions so an older in-flight request cannot restore them."""
    with closing(_connect()) as conn, conn:
        if (
            expected_generation is not None
            and _generation(conn, chat_id) != expected_generation
        ):
            return None
        revision = _advance_revision(conn, chat_id, expected_revision)
        if revision is not None:
            conn.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
            if reset:
                _generation(conn, chat_id)
                conn.execute(
                    "UPDATE session_generations SET generation=generation+1 WHERE chat_id=?",
                    (chat_id,),
                )
    return revision


def reserve_message(message_id, event):
    """Bind an incoming ID to its original route and payload, never a processing tombstone.

    In-process active claims serialize execution. Durable retryable reservations
    can be reclaimed after a restart; completed results can only be redelivered.
    """
    identity = (
        event.chat_id,
        event.chat_type,
        event.sender_id,
        hashlib.sha256(event.text.encode("utf-8")).hexdigest(),
    )
    with closing(_connect()) as conn, conn:
        conn.row_factory = sqlite3.Row
        generation = _generation(conn, event.chat_id)
        row = conn.execute(
            "SELECT * FROM message_receipts WHERE message_id=?", (message_id,)
        ).fetchone()
        if row is not None:
            if (
                tuple(
                    row[key]
                    for key in ("chat_id", "chat_type", "sender_id", "payload_hash")
                )
                != identity
            ):
                raise ValueError(
                    "Incoming message ID reused with a different route or payload"
                )
        else:
            conn.execute(
                "INSERT INTO message_receipts "
                "(message_id,chat_id,chat_type,sender_id,payload_hash,generation,state) "
                "VALUES (?,?,?,?,?,?, 'retryable')",
                (message_id, *identity, generation),
            )
            row = conn.execute(
                "SELECT * FROM message_receipts WHERE message_id=?", (message_id,)
            ).fetchone()
        return dict(row)


def finish_message(message_id, state, *, answer=None, delivery_ids=None):
    """Record completion before delivery, rejecting results from an older reset."""
    with closing(_connect()) as conn, conn:
        # Start a write transaction before the read so completion and clear serialize.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT chat_id,generation FROM message_receipts WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Missing incoming message reservation")
        if _generation(conn, row[0]) != row[1]:
            state = "cancelled"
        conn.execute(
            "UPDATE message_receipts SET state=?,answer=COALESCE(?,answer),"
            "delivery_ids=COALESCE(?,delivery_ids) WHERE message_id=?",
            (
                state,
                answer,
                json.dumps(delivery_ids) if delivery_ids is not None else None,
                message_id,
            ),
        )
    return state


def clear_message(message_id, answer):
    """Commit a reset and its cached acknowledgement as one durable operation."""
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT chat_id,generation,state FROM message_receipts WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if row is None or row[2] != "retryable":
            raise ValueError("Reset requires a retryable message reservation")
        chat_id, generation, _ = row
        if _generation(conn, chat_id) != generation:
            conn.execute(
                "UPDATE message_receipts SET state='cancelled' WHERE message_id=?",
                (message_id,),
            )
            return None
        _advance_revision(conn, chat_id, None)
        conn.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
        conn.execute(
            "UPDATE session_generations SET generation=generation+1 WHERE chat_id=?",
            (chat_id,),
        )
        generation += 1
        conn.execute(
            "UPDATE message_receipts SET generation=?,state='completed',answer=? WHERE message_id=?",
            (generation, answer, message_id),
        )
        return generation
