"""Transactional, deduplicated inbox for the optional HTTP event transport."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


def _connect(root: Path):
    path = root / "data" / "webhook_inbox.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS inbox (message_id TEXT PRIMARY KEY, payload TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)")
    conn.commit()
    return conn


def enqueue(root: Path, message: dict) -> None:
    with closing(_connect(root)) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO inbox(message_id, payload) VALUES (?, ?)",
                     (message["message_id"], json.dumps(message, ensure_ascii=False)))


def drain(root: Path) -> list[dict]:
    with closing(_connect(root)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT message_id, payload FROM inbox WHERE consumed=0 ORDER BY rowid LIMIT 100").fetchall()
        conn.executemany("UPDATE inbox SET consumed=1 WHERE message_id=?", [(row[0],) for row in rows])
        return [json.loads(row[1]) for row in rows]
