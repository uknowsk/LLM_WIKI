"""SQLite job table plus a single serialized compile worker.

Engine index/log files and the store are shared state, so every job runs while holding the
process-wide COMPILE_LOCK: concurrent drops or threads can never interleave wiki writes.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from llmwiki.config import Settings
from llmwiki.pipeline import inbox, notes
from llmwiki.pipeline.run import ProcessResult

COMPILE_LOCK = threading.Lock()
MAX_ATTEMPTS = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    space TEXT NOT NULL,
    status TEXT NOT NULL,           -- new | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobQueue:
    def __init__(self, settings: Settings, max_attempts: int = MAX_ATTEMPTS):
        self.settings, self.max_attempts = settings, max_attempts
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(settings.db_path), timeout=30, check_same_thread=False)
        self._lock = threading.Lock()  # guards this connection; never held while compiling
        self._given_up: dict[str, tuple[int, int]] = {}  # path -> (size, mtime_ns) of a file we gave up on but could not move
        self._db.execute(_SCHEMA)
        self._db.commit()

    def enqueue(self, path: Path, space: str) -> bool:
        """Add a job. No-op while a job for this path is pending or still retriable; returns True if (re)queued."""
        key, now = str(Path(path)), _now()
        sig = self._sig(Path(path))
        if key in self._given_up:
            if sig == self._given_up[key]:
                return False  # unchanged since we gave up (and could not move it away): do not start over forever
            del self._given_up[key]
        with self._lock:
            row = self._db.execute("SELECT status, attempts FROM pipeline_jobs WHERE path = ?", (key,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO pipeline_jobs (path, space, status, created_at, updated_at) VALUES (?, ?, 'new', ?, ?)",
                    (key, space, now, now),
                )
            elif row[0] == "new" or (row[0] == "failed" and row[1] < self.max_attempts):
                return False
            else:  # a finished/exhausted path that is dropped again is a new file
                self._db.execute(
                    "UPDATE pipeline_jobs SET space=?, status='new', attempts=0, last_error='', updated_at=? WHERE path=?",
                    (space, now, key),
                )
            self._db.commit()
        return True

    @staticmethod
    def _sig(path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
            return st.st_size, st.st_mtime_ns
        except OSError:
            return None

    def _remember_given_up(self, path: Path) -> None:
        sig = self._sig(path)  # only matters if the file is still there (the move failed)
        if sig is not None:
            self._given_up[str(path)] = sig

    def drop_pending(self) -> None:
        """At startup: forget queued-but-unstarted jobs; the next scan re-derives path and space from the folder."""
        with self._lock:
            self._db.execute("DELETE FROM pipeline_jobs WHERE status = 'new'")
            self._db.commit()

    def jobs(self, status: str | None = None) -> list[dict]:
        with self._lock:
            return self._jobs(status)

    def _jobs(self, status: str | None) -> list[dict]:
        q = "SELECT path, space, status, attempts, last_error, created_at, updated_at FROM pipeline_jobs"
        args: tuple = ()
        if status:
            q, args = q + " WHERE status = ?", (status,)
        cols = ("path", "space", "status", "attempts", "last_error", "created_at", "updated_at")
        return [dict(zip(cols, r)) for r in self._db.execute(q + " ORDER BY id", args)]

    def _finish(self, key: str, status: str, attempts: int, error: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE pipeline_jobs SET status=?, attempts=?, last_error=?, updated_at=? WHERE path=?",
                (status, attempts, error, _now(), key),
            )
            self._db.commit()

    def run_pending(self, process: Callable[[Path, str], ProcessResult], max_jobs: int | None = None) -> list[ProcessResult]:
        """Run each runnable job once, one at a time. Failed jobs are retried on the next call (attempt cap)."""
        results: list[ProcessResult] = []
        with COMPILE_LOCK:
            with self._lock:
                rows = self._db.execute(
                    "SELECT path, space, attempts FROM pipeline_jobs "
                    "WHERE status='new' OR (status='failed' AND attempts < ?) ORDER BY id",
                    (self.max_attempts,),
                ).fetchall()
            for key, space, attempts in rows[:max_jobs]:
                path = Path(key)
                if not path.exists():
                    self._finish(key, "failed", self.max_attempts, "file vanished before processing")
                    continue
                res = process(path, space)
                results.append(res)
                if res.status in ("done", "skipped"):
                    self._finish(key, "done", attempts + 1, "; ".join(res.attachment_errors))
                elif res.status == "locked":  # in use / still growing: not a failed attempt, retried next scan
                    self._finish(key, "new", attempts, "in use or still being written; will retry")
                elif res.status == "rejected":  # terminal, never retried (process_file already moved the file)
                    self._finish(key, "failed", self.max_attempts, res.error)
                    self._remember_given_up(path)
                else:
                    attempts += 1
                    self._finish(key, "failed", attempts, res.error)
                    if attempts >= self.max_attempts:
                        inbox.mark_failed(self.settings, path, space, res.error)
                        inbox.notify(self.settings, path, notes.categorize_error(res.error))
                        self._remember_given_up(path)
        return results

    def give_up(self, path: Path, space: str, reason: str, category: str) -> None:
        """Finalize a file that stayed locked/unreadable too long: _failed + note, job marked exhausted."""
        key = str(Path(path))
        with self._lock:
            row = self._db.execute("SELECT 1 FROM pipeline_jobs WHERE path = ?", (key,)).fetchone()
        if row:
            self._finish(key, "failed", self.max_attempts, reason)
        inbox.mark_failed(self.settings, Path(path), space, reason)
        inbox.notify(self.settings, Path(path), category)
        self._remember_given_up(Path(path))

    def drain(self, process: Callable[[Path, str], ProcessResult]) -> list[ProcessResult]:
        """Run until nothing runnable is left (used by --once): includes the retries."""
        out: list[ProcessResult] = []
        for _ in range(self.max_attempts):
            batch = self.run_pending(process)
            if not batch:
                break
            out += batch
        return out

    def close(self) -> None:
        self._db.close()
