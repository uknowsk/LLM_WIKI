"""In-app abuse limits: token-bucket rate limiter and an audit wrapper that bounds table growth."""
from __future__ import annotations

import threading
from collections.abc import Callable

from ..auth import User

_MAX_KEYS = 20000
_MAX_TARGET = 200
_MAX_DETAIL = 500
_COALESCED = ("login_failed", "read_denied")
_COALESCE_WINDOW = 60.0


class RateLimiter:
    """Token bucket per key: `per_minute` tokens refilled continuously, burst == per_minute."""

    def __init__(self, clock: Callable[[], float]):
        self._clock, self._lock, self._buckets = clock, threading.Lock(), {}

    def allow(self, key: str, per_minute: int) -> bool:
        now, rate = self._clock(), per_minute / 60.0
        with self._lock:
            tokens, last = self._buckets.get(key, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + max(0.0, now - last) * rate)
            ok = tokens >= 1.0
            if len(self._buckets) >= _MAX_KEYS and key not in self._buckets:
                self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < 60.0}
            self._buckets[key] = (tokens - 1.0 if ok else tokens, now)
            return ok


class AuditGuard:
    """Wraps AuditLog.record: truncates stored strings and coalesces repeated identical
    login_failed/read_denied rows (same user+target) within a window into one row plus a counter."""

    def __init__(self, audit, clock: Callable[[], float]):
        self._audit, self._clock, self._lock, self._seen = audit, clock, threading.Lock(), {}

    def __getattr__(self, name):
        if name in ("prune", "count_older_than"):  # destructive admin tools are never reachable from the web layer
            raise AttributeError(name)
        return getattr(self._audit, name)

    def record(self, user: User, action: str, target: str, detail: str = "") -> None:
        target, detail = str(target)[:_MAX_TARGET], str(detail)[:_MAX_DETAIL]
        if action in _COALESCED:
            key, now = (user.id, action, target), self._clock()
            with self._lock:
                if len(self._seen) >= _MAX_KEYS:
                    self._seen = {k: v for k, v in self._seen.items() if now - v[0] < _COALESCE_WINDOW}
                first, count = self._seen.get(key, (now, 0))
                if key in self._seen and now - first < _COALESCE_WINDOW:
                    self._seen[key] = (first, count + 1)
                    return
                self._seen[key] = (now, 0)
            if count:
                detail = f"{detail} repeats_suppressed={count}".strip()
        self._audit.record(user, action, target, detail)
