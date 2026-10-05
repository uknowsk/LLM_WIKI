"""Background ingest thread: scan <home>/inbox (everything there is space 'personal') and the watch folders, then run
the central pipeline's JobQueue/process_file unchanged. Status counts only (no names, no content)."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..config import Settings
from ..pipeline import inbox
from ..pipeline.queue import JobQueue
from ..pipeline.run import ProcessResult
from .identity import PERSONAL_SPACE
from .watchfolders import WatchScanner

log = logging.getLogger("llmwiki.personal")
_HIDDEN_SYSTEM = inbox.FILE_ATTRIBUTE_HIDDEN | inbox.FILE_ATTRIBUTE_SYSTEM


def scan_inbox(settings: Settings) -> list[Path]:
    """Files below <home>/inbox (any depth) except reserved folders, hidden/system, temp/lock files and links."""
    out: list[Path] = []
    root = inbox.inbox_dir(settings)
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if d == root and e.name in inbox.RESERVED:
                continue
            try:
                attrs = getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0)
                if attrs & _HIDDEN_SYSTEM or e.is_symlink() or attrs & inbox.FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False) and not inbox.is_temp_name(e.name):
                    out.append(Path(e.path))
            except OSError:
                continue
    return out


class IngestWorker:
    def __init__(self, settings: Settings, queue: JobQueue, process: Callable[[Path, str], ProcessResult],
                 watcher: WatchScanner | None = None, *, interval: float = 3.0, watch_interval: float = 60.0,
                 min_age: float = 2.0):
        self.settings, self.queue, self._process = settings, queue, process
        self.watcher, self.interval, self.watch_interval, self.min_age = watcher, interval, watch_interval, min_age
        self._sigs: dict[str, tuple[int, int]] = {}
        self._next_watch = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _ready(self, path: Path, seen: dict[str, tuple[int, int]]) -> bool:
        try:
            st = path.stat()
            sig = (st.st_size, st.st_mtime_ns)
            seen[str(path)] = sig
            if 0 <= time.time() - st.st_mtime < self.min_age or self._sigs.get(str(path)) != sig:
                return False
            with open(path, "rb"):
                pass
        except OSError:
            return False
        return True

    def scan_once(self) -> dict[str, int]:
        counts = {"queued": 0, "waiting": 0, "watch_staged": 0}
        if self.watcher is not None and time.monotonic() >= self._next_watch:
            scanned = self.watcher.scan()
            counts["watch_staged"] = scanned["staged"]
            # files that are still being saved (or a full staging folder) are looked at again soon, not in a minute
            self._next_watch = time.monotonic() + (self.interval if scanned["waiting"] else self.watch_interval)
        seen: dict[str, tuple[int, int]] = {}
        for path in scan_inbox(self.settings):
            if not self._ready(path, seen):
                counts["waiting"] += 1
            elif self.queue.enqueue(path, PERSONAL_SPACE):
                counts["queued"] += 1
        self._sigs = seen
        return counts

    def _guarded(self, path: Path, space: str) -> ProcessResult:
        if self.stopped:  # shutting down: leave the job for the next start (not a failed attempt)
            return ProcessResult("locked", path, space, error="stopped")
        self._running = True
        try:
            return self._process(path, space)
        finally:
            self._running = False

    def run_once(self) -> None:
        self.scan_once()
        self.queue.run_pending(self._guarded)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 (a broken cycle must not kill the worker)
                log.error("ingest cycle failed: %s", type(exc).__name__)
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="personal-ingest", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def status(self) -> dict:
        """Counts only. pending = queued or retriable; failed = gave up (incl. rejected files)."""
        db = sqlite3.connect(str(self.settings.db_path), timeout=5.0)
        try:
            rows = db.execute(
                "SELECT status, attempts >= ?, COUNT(*) FROM pipeline_jobs WHERE status IN ('new', 'failed') "
                "GROUP BY status, attempts >= ?", (self.queue.max_attempts, self.queue.max_attempts)).fetchall()
        except sqlite3.OperationalError:  # table not created yet
            rows = []
        finally:
            db.close()
        pending = sum(n for st, spent, n in rows if st == "new" or not spent)
        failed = sum(n for st, spent, n in rows if st == "failed" and spent)
        return {"pending": pending, "failed": failed, "working": self._running or pending > 0}
