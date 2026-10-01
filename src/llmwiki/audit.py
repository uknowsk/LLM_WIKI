"""Audit log: who did what, when. Single entry point for all tracked actions."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .auth import User

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
)
"""


class AuditLog:
    def __init__(self, db_path: Path | str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5.0)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)
        self._db.commit()

    def record(self, user: User, action: str, target: str, detail: str = "") -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._db.execute(
                "INSERT INTO audit (ts, user_id, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                (ts, user.id, action, target, detail),
            )
            self._db.commit()

    def entries(self, user_id: str | None = None) -> list[tuple]:
        q = "SELECT ts, user_id, action, target, detail FROM audit"
        args: tuple = ()
        if user_id is not None:
            q += " WHERE user_id = ?"
            args = (user_id,)
        with self._lock:
            return self._db.execute(q + " ORDER BY id", args).fetchall()

    def close(self) -> None:
        with self._lock:
            self._db.close()
