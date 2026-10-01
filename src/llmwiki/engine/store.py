"""SQLite metadata. Article ACL is DERIVED from the spaces of its source raws, never stored."""
from __future__ import annotations

import sqlite3
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
    def __init__(self, db_path: Path | str):
        self._db = sqlite3.connect(str(db_path))
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def add_raw(self, rec: RawRecord) -> None:
        if not rec.space:
            raise ValueError("raw record without a space is refused (fail closed)")
        self._db.execute(
            "INSERT OR REPLACE INTO raw_files (raw_path, space, sha256) VALUES (?, ?, ?)",
            (rec.raw_path, rec.space, rec.sha256),
        )
        self._db.commit()

    def upsert_article(self, path: str, title: str, sources: Iterable[str]) -> None:
        updated = datetime.now(timezone.utc).date().isoformat()
        self._db.execute("INSERT OR REPLACE INTO articles (path, title, updated) VALUES (?, ?, ?)", (path, title, updated))
        for s in sources:
            self._db.execute("INSERT OR IGNORE INTO article_sources (article_path, raw_path) VALUES (?, ?)", (path, s))
        self._db.commit()

    def article_title(self, path: str) -> str | None:
        row = self._db.execute("SELECT title FROM articles WHERE path = ?", (path,)).fetchone()
        return row[0] if row else None

    def article_paths(self) -> list[str]:
        return [r[0] for r in self._db.execute("SELECT path FROM articles ORDER BY path")]

    def article_sources(self, path: str) -> list[str]:
        rows = self._db.execute("SELECT raw_path FROM article_sources WHERE article_path = ? ORDER BY raw_path", (path,))
        return [r[0] for r in rows]

    def article_spaces(self, path: str) -> frozenset[str]:
        """Spaces of all sources. Empty (=> denied by acl.can_read) if no sources or any source is unlabeled."""
        rows = self._db.execute(
            "SELECT s.raw_path, r.space FROM article_sources s LEFT JOIN raw_files r ON r.raw_path = s.raw_path "
            "WHERE s.article_path = ?",
            (path,),
        ).fetchall()
        if not rows or any(space is None or space == "" for _, space in rows):
            return frozenset()
        return frozenset(space for _, space in rows)

    def articles_with_spaces(self, spaces: frozenset[str]) -> list[str]:
        return [p for p in self.article_paths() if self.article_spaces(p) == spaces]

    def close(self) -> None:
        self._db.close()
