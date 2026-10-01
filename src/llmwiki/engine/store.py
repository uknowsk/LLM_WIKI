"""SQLite metadata. Article ACL is DERIVED from the spaces of its source raws, never stored."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from ..models import RawRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_files (raw_path TEXT PRIMARY KEY, space TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS articles (path TEXT PRIMARY KEY, title TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS article_sources (
    article_path TEXT NOT NULL, raw_path TEXT NOT NULL, PRIMARY KEY (article_path, raw_path)
);
"""


class Store:
    """Thread-safe (one shared connection guarded by an RLock, WAL for concurrent processes)."""

    def __init__(self, db_path: Path | str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5.0)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def _q(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def add_raw(self, rec: RawRecord) -> None:
        """Register a raw file. Re-registering is allowed only with the identical (space, sha256):
        a silent re-label would retroactively change the ACL of every article built from it."""
        if not rec.space:
            raise ValueError("raw record without a space is refused (fail closed)")
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO raw_files (raw_path, space, sha256) VALUES (?, ?, ?)",
                    (rec.raw_path, rec.space, rec.sha256),
                )
                self._db.commit()
            except sqlite3.IntegrityError:
                self._db.rollback()
                row = self._db.execute("SELECT space, sha256 FROM raw_files WHERE raw_path = ?", (rec.raw_path,)).fetchone()
                if row != (rec.space, rec.sha256):
                    raise ValueError(f"raw file {rec.raw_path!r} is already registered with a different space/hash") from None

    def raw_space(self, raw_path: str) -> str | None:
        rows = self._q("SELECT space FROM raw_files WHERE raw_path = ?", (raw_path,))
        return rows[0][0] if rows else None

    def upsert_article(self, path: str, title: str, sources: Iterable[str], space: str | None = None) -> None:
        """Sources must be `raw/...` paths registered in raw_files (and, if `space` is given, in that space)."""
        sources = list(dict.fromkeys(sources))
        for s in sources:
            sp = self.raw_space(s) if s.startswith("raw/") and ".." not in s.split("/") else None
            if not sp or (space is not None and sp != space):
                raise ValueError(f"source {s!r} is not a registered raw file of the expected space")
        updated = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO articles (path, title, updated) VALUES (?, ?, ?)", (path, title, updated))
            for s in sources:
                self._db.execute("INSERT OR IGNORE INTO article_sources (article_path, raw_path) VALUES (?, ?)", (path, s))
            self._db.commit()

    def article_title(self, path: str) -> str | None:
        rows = self._q("SELECT title FROM articles WHERE path = ?", (path,))
        return rows[0][0] if rows else None

    def article_paths(self) -> list[str]:
        return [r[0] for r in self._q("SELECT path FROM articles ORDER BY path")]

    def article_sources(self, path: str) -> list[str]:
        rows = self._q("SELECT raw_path FROM article_sources WHERE article_path = ? ORDER BY raw_path", (path,))
        return [r[0] for r in rows]

    def article_spaces(self, path: str) -> frozenset[str]:
        """Spaces of all sources. Empty (=> denied by acl.can_read) if no sources or any source is unlabeled."""
        rows = self._q(
            "SELECT s.raw_path, r.space FROM article_sources s LEFT JOIN raw_files r ON r.raw_path = s.raw_path "
            "WHERE s.article_path = ?",
            (path,),
        )
        if not rows or any(space is None or space == "" for _, space in rows):
            return frozenset()
        return frozenset(space for _, space in rows)

    def articles_with_spaces(self, spaces: frozenset[str]) -> list[str]:
        return [p for p in self.article_paths() if self.article_spaces(p) == spaces]

    def close(self) -> None:
        with self._lock:
            self._db.close()
