"""SQLite response cache for LLM calls: key = sha256(model, system, prompt, temperature).

Only successful replies are stored. The original call latency is stored too, so a cached rerun can still
report realistic latency (`saved_ms` accumulates original-minus-actual time).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from ..engine.llm import LLMClient

_SCHEMA = "CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, reply TEXT NOT NULL, latency_ms REAL NOT NULL)"


def cache_key(model: str, system: str, prompt: str, temperature: float | None) -> str:
    return hashlib.sha256(json.dumps([model, system, prompt, temperature], ensure_ascii=False).encode("utf-8")).hexdigest()


class CachedLLM:
    def __init__(self, inner: LLMClient, db_path: Path | str, model: str = ""):
        self.inner, self.model = inner, model or getattr(inner, "model", "")
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)
        self._db.commit()
        self.hits = self.misses = 0
        self.saved_ms = 0.0

    def complete(self, system: str, prompt: str, temperature: float | None = None) -> str:
        key = cache_key(self.model, system, prompt, temperature)
        with self._lock:
            row = self._db.execute("SELECT reply, latency_ms FROM llm_cache WHERE key = ?", (key,)).fetchone()
        if row is not None:
            self.hits += 1
            self.saved_ms += row[1]  # the real call would have taken this long (the cached read costs ~0)
            return row[0]
        t0 = time.perf_counter()
        reply = self.inner.complete(system, prompt, temperature)  # errors propagate and are NOT cached
        ms = (time.perf_counter() - t0) * 1000
        self.misses += 1
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO llm_cache VALUES (?, ?, ?)", (key, reply, ms))
            self._db.commit()
        return reply

    def close(self) -> None:
        with self._lock:
            self._db.close()
