"""Polling inbox watcher (no external deps): scan_once() enqueues, run_forever() loops."""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from llmwiki.audit import AuditLog
from llmwiki.config import Settings
from llmwiki.pipeline import inbox, notes
from llmwiki.pipeline.aliases import AliasMap
from llmwiki.pipeline.queue import JobQueue
from llmwiki.pipeline.run import SYSTEM_USER, ProcessResult

log = logging.getLogger("llmwiki.pipeline")


def locked_max_scans(environ=None) -> int:
    """WIKI_LOCKED_MAX_SCANS: consecutive scans a file may stay locked (unchanged) before it is given up. Default 60."""
    try:
        v = int((environ if environ is not None else os.environ).get("WIKI_LOCKED_MAX_SCANS", ""))
        return v if v > 0 else 60
    except ValueError:
        return 60


def _positive_env(name: str, default: int, environ=None) -> int:
    try:
        v = int((environ if environ is not None else os.environ).get(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


def max_pending_jobs(environ=None) -> int:
    """WIKI_MAX_PENDING_JOBS: queue length at which new files are left in the inbox for a later scan. Default 500."""
    return _positive_env("WIKI_MAX_PENDING_JOBS", 500, environ)


def batch_jobs(environ=None) -> int:
    """WIKI_BATCH_JOBS: jobs run per loop iteration before the inbox is rescanned (fairness between spaces). Default 10."""
    return _positive_env("WIKI_BATCH_JOBS", 10, environ)


class Watcher:
    def __init__(self, settings: Settings, queue: JobQueue, audit: AuditLog, min_age: float = 0.0,
                 aliases: AliasMap | None = None, locked_max: int | None = None, max_pending: int | None = None,
                 batch: int | None = None):
        self.settings, self.queue, self.audit, self.min_age = settings, queue, audit, min_age
        self.aliases = aliases
        self.locked_max = locked_max if locked_max else locked_max_scans()
        self.max_pending = max_pending if max_pending else max_pending_jobs()
        self.batch = batch if batch else batch_jobs()
        self.deferred = 0  # files left in the inbox by the last scan because the queue was full
        self._locks: dict[str, tuple[tuple[int, int], int]] = {}  # path -> (signature, consecutive locked scans)
        self._sigs: dict[str, tuple[int, int]] = {}  # (size, mtime_ns) seen on the previous scan
        self._seen: dict[str, tuple[int, int]] = {}
        self._reported: set[str] = set()  # unmovable rejects (e.g. linked folders): report once, not every scan

    def _ready(self, item: inbox.InboxItem, now: float) -> bool:
        """Not too young, unchanged since the previous scan (when min_age is on) and openable (not locked).
        A file that stays locked and unchanged for `locked_max` consecutive scans is given up (_failed + note)."""
        path, key = item.path, str(item.path)
        try:
            st = path.stat()
            sig = (st.st_size, st.st_mtime_ns)
            stable = not self.min_age or self._sigs.get(key) == sig
            self._seen[key] = sig
            if self.min_age and now - st.st_mtime < self.min_age:
                return False
            if not stable:
                return False
            with open(path, "rb"):
                pass
        except PermissionError as exc:
            if not inbox.is_sharing_violation(exc):
                return True  # access denied: let processing fail normally (consumes attempts, 3 strikes -> _failed)
            last_sig, n = self._locks.get(key, (sig, 0))
            n = n + 1 if last_sig == sig else 1  # a changing file is "growing", not "stuck"
            self._locks[key] = (sig, n)
            if n >= self.locked_max:
                self._give_up(item)
            return False
        except OSError:  # vanished: try again on the next scan, no failed attempt
            return False
        self._locks.pop(key, None)
        return True

    def _give_up(self, item: inbox.InboxItem) -> None:
        self._locks.pop(str(item.path), None)
        reason = f"still locked or unreadable after {self.locked_max} scans"
        self.queue.give_up(item.path, item.space or "", reason, notes.NO_ACCESS)
        self.audit.record(SYSTEM_USER, "ingest_fail", f"{item.space}/{item.path.name}", reason)

    def _reject(self, item: inbox.InboxItem) -> bool:
        """Report a reject. Returns False when it was already reported (nothing to count)."""
        key = str(item.path)
        if key in self._reported:
            return False
        reason = item.reason or "no space"
        moved = None
        if not item.is_dir:  # a linked folder is never renamed or followed
            try:
                moved = inbox.reject(self.settings, item.path, None, reason)
            except OSError as exc:
                log.warning("reject move failed (%s)", type(exc).__name__)
        if moved is None:
            self._reported.add(key)
        self.audit.record(SYSTEM_USER, "ingest_reject", item.path.name, reason)
        inbox.notify(self.settings, item.path, item.category or notes.FAILED)
        return True

    def scan_once(self) -> dict[str, int]:
        """Reject files outside a valid space folder; enqueue the rest. Files younger than `min_age`
        seconds, still changing or locked by another process are left for the next scan."""
        counts = {"queued": 0, "rejected": 0, "waiting": 0}
        now = time.time()
        self._seen = {}
        self.deferred = 0
        pending = self.queue.pending_count()
        for item in inbox.scan(self.settings, self.aliases):
            if item.space is None:
                counts["rejected"] += self._reject(item)
            elif not self._ready(item, now):
                counts["waiting"] += 1
            elif pending >= self.max_pending:
                self.deferred += 1  # queue is full: the file stays untouched in the inbox until there is room
            elif self.queue.enqueue(item.path, item.space):
                counts["queued"] += 1
                pending += 1
        if self.deferred:
            log.warning("job queue full (%d pending): %d file(s) left in the inbox for a later scan", pending, self.deferred)
        self._sigs = self._seen
        return counts

    def run_forever(
        self, process: Callable[[Path, str], ProcessResult], interval: float = 5.0,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        fails = 0
        while not should_stop():
            try:
                self.scan_once()
                self.queue.run_pending(process, max_jobs=self.batch)  # small batches, then rescan: spaces get fair turns
                fails = 0
            except Exception as exc:  # noqa: BLE001  one bad input must never kill the service loop
                fails += 1
                log.error("pipeline iteration failed (%s), retry %d", type(exc).__name__, fails)  # no names/content
                time.sleep(min(60.0, interval * 2 ** min(fails, 6)))
            time.sleep(interval)
