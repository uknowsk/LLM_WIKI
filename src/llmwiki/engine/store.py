"""SQLite metadata. Article ACL is DERIVED from the spaces of its source raws, never stored."""
from __future__ import annotations

import json
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
CREATE TABLE IF NOT EXISTS article_revs (path TEXT PRIMARY KEY, rev INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS article_embeddings (
    article_path TEXT PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL,
    text_sha256 TEXT NOT NULL, vector BLOB NOT NULL
);
"""

# Article readable iff it has sources and EVERY source raw is labeled with a space the user holds
# (single-query equivalent of acl.can_read(user, Store.article_spaces(path)); fail closed).
_READABLE_SQL = (
    "SELECT a.path, COALESCE(v.rev, 0) FROM articles a LEFT JOIN article_revs v ON v.path = a.path "
    "WHERE EXISTS (SELECT 1 FROM article_sources s WHERE s.article_path = a.path) "
    "AND NOT EXISTS (SELECT 1 FROM article_sources s LEFT JOIN raw_files r ON r.raw_path = s.raw_path "
    "WHERE s.article_path = a.path AND (r.space IS NULL OR r.space = '' "
    "OR r.space NOT IN (SELECT value FROM json_each(?)))) ORDER BY a.path"
)


class Store:
    """Thread-safe (one shared connection guarded by an RLock, WAL for concurrent processes)."""

    def __init__(self, db_path: Path | str):
        self._lock = threading.RLock()
        self._local_version = 0
        self.cache: dict = {}  # per-process retrieval caches, keyed to `version` (see engine/retrieval.py)
        self._db =sqlite3.connect(str(db_path), check_same_thread=False, timeout=5.0)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    @property
    def version(self) -> tuple[int, int]:
        """Changes on ANY write through this Store (local counter) or by another connection/process
        (SQLite data_version). Caches derived from the DB must be keyed by it."""
        with self._lock:
            return self._local_version, self._db.execute("PRAGMA data_version").fetchone()[0]

    def _bump(self) -> None:
        self._local_version += 1

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
                self._bump()
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
            self._db.execute("INSERT INTO article_revs (path, rev) VALUES (?, 1) "
                             "ON CONFLICT(path) DO UPDATE SET rev = rev + 1", (path,))
            for s in sources:
                self._db.execute("INSERT OR IGNORE INTO article_sources (article_path, raw_path) VALUES (?, ?)", (path, s))
            self._db.commit()
            self._bump()

    def delete_article(self, path: str) -> None:
        """Remove an article with its sources and embedding (its revision counter survives) (the file is the caller's business)."""
        with self._lock:
            # article_revs is kept on purpose: a re-created path must get a NEW revision (cache keys)
            for t, col in (("articles", "path"), ("article_sources", "article_path"),
                           ("article_embeddings", "article_path")):
                self._db.execute(f"DELETE FROM {t} WHERE {col} = ?", (path,))
            self._db.commit()
            self._bump()

    def relabel_raw(self, raw_path: str, space: str) -> None:
        """Explicit admin re-label: changes the ACL of every article built from this raw."""
        if not space:
            raise ValueError("a space is mandatory (fail closed)")
        with self._lock:
            self._db.execute("UPDATE raw_files SET space = ? WHERE raw_path = ?", (space, raw_path))
            self._db.commit()
            self._bump()

    def readable_articles(self, user_spaces: Iterable[str]) -> dict[str, int]:
        """{article path: revision} readable with `user_spaces`, in ONE query. Equals the
        acl.can_read(user, article_spaces(p)) oracle (tested on randomized data)."""
        rows = self._q(_READABLE_SQL, (json.dumps(sorted(set(user_spaces))),))
        return {p: rev for p, rev in rows}

    # -- embeddings (normalized float32 blobs; `text_sha256` identifies the embedded text) --
    def set_embedding(self, path: str, model: str, dim: int, text_sha256: str, vector: bytes) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO article_embeddings VALUES (?, ?, ?, ?, ?)",
                             (path, model, dim, text_sha256, vector))
            self._db.commit()
            self._bump()

    def delete_embedding(self, path: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM article_embeddings WHERE article_path = ?", (path,))
            self._db.commit()
            self._bump()

    def embedding_meta(self, paths: Iterable[str] | None = None) -> dict[str, tuple[str, int, str]]:
        """{path: (model, dim, text_sha256)}, restricted to `paths` when given (vectors not loaded)."""
        sql = "SELECT article_path, model, dim, text_sha256 FROM article_embeddings"
        if paths is None:
            rows = self._q(sql)
        else:
            rows = self._q(sql + " WHERE article_path IN (SELECT value FROM json_each(?))",
                           (json.dumps(sorted(set(paths))),))
        return {p: (m, d, h) for p, m, d, h in rows}

    def embedding_vectors(self, paths: Iterable[str]) -> dict[str, bytes]:
        rows = self._q("SELECT article_path, vector FROM article_embeddings "
                       "WHERE article_path IN (SELECT value FROM json_each(?))", (json.dumps(sorted(set(paths))),))
        return {p: bytes(v) for p, v in rows}

    def article_title(self, path: str) -> str | None:
        rows = self._q("SELECT title FROM articles WHERE path = ?", (path,))
        return rows[0][0] if rows else None

    def article_meta(self, paths: Iterable[str], limit: int) -> list[tuple[str, str, str]]:
        """(path, title, updated) of the given paths, most recently updated first. Metadata only, no ACL decision here:
        callers must pass paths that already went through readable_articles."""
        return [tuple(r) for r in self._q(
            "SELECT path, title, updated FROM articles WHERE path IN (SELECT value FROM json_each(?)) "
            "ORDER BY updated DESC, path LIMIT ?", (json.dumps(sorted(set(paths))), limit))]

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
