"""Audit log: who did what, when. Single entry point for all tracked actions."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
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

    MIN_PRUNE_DAYS = 30
    ADMIN_USER = User(id="system:audit_admin", name="audit_admin", department="system")

    @staticmethod
    def _iso(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")

    def entries_since(self, since_iso: str) -> list[tuple]:
        """Rows with ts >= since (ISO 8601; naive means UTC). Raises ValueError on a bad timestamp."""
        cut = self._iso(datetime.fromisoformat(since_iso))
        with self._lock:
            return self._db.execute("SELECT ts, user_id, action, target, detail FROM audit WHERE ts >= ? ORDER BY id",
                                    (cut,)).fetchall()

    def count_older_than(self, days: int, now: datetime | None = None) -> int:
        cut = self._iso((now or datetime.now(timezone.utc)) - timedelta(days=days))
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM audit WHERE ts < ?", (cut,)).fetchone()[0]

    def prune(self, older_than_days: int, now: datetime | None = None) -> int:
        """ADMIN TOOL ONLY (llmwiki.audit_admin); never called automatically and not reachable from the web layer.
        Deletes rows older than the cutoff, then records one 'audit_prune' row (count + cutoff). Refuses < 30 days."""
        if isinstance(older_than_days, bool) or not isinstance(older_than_days, int) or older_than_days < self.MIN_PRUNE_DAYS:
            raise ValueError(f"refusing to prune audit rows newer than {self.MIN_PRUNE_DAYS} days")
        cutoff = self._iso((now or datetime.now(timezone.utc)) - timedelta(days=older_than_days))
        with self._lock:
            n = self._db.execute("DELETE FROM audit WHERE ts < ?", (cutoff,)).rowcount
            self._db.commit()
            self.record(self.ADMIN_USER, "audit_prune", "audit", f"deleted={n} cutoff={cutoff}")
        return n

    def close(self) -> None:
        with self._lock:
            self._db.close()
