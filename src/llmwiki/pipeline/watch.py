"""Polling inbox watcher (no external deps): scan_once() enqueues, run_forever() loops."""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from llmwiki.audit import AuditLog
from llmwiki.config import Settings
from llmwiki.pipeline import inbox
from llmwiki.pipeline.queue import JobQueue
from llmwiki.pipeline.run import SYSTEM_USER, ProcessResult


class Watcher:
    def __init__(self, settings: Settings, queue: JobQueue, audit: AuditLog, min_age: float = 0.0):
        self.settings, self.queue, self.audit, self.min_age = settings, queue, audit, min_age

    def scan_once(self) -> dict[str, int]:
        """Reject files outside a valid space folder; enqueue the rest. Files younger than
        `min_age` seconds are left for the next scan (still being copied)."""
        counts = {"queued": 0, "rejected": 0, "waiting": 0}
        now = time.time()
        for item in inbox.scan(self.settings):
            if item.space is None:
                reason = item.reason or "no space"
                inbox.reject(self.settings, item.path, None, reason)
                self.audit.record(SYSTEM_USER, "ingest_reject", item.path.name, reason)
                counts["rejected"] += 1
            elif self.min_age and now - item.path.stat().st_mtime < self.min_age:
                counts["waiting"] += 1
            elif self.queue.enqueue(item.path, item.space):
                counts["queued"] += 1
        return counts

    def run_forever(
        self, process: Callable[[Path, str], ProcessResult], interval: float = 5.0,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        while not should_stop():
            self.scan_once()
            self.queue.run_pending(process)
            time.sleep(interval)
