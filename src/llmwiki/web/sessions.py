"""Server-side sessions and the SessionAuth boundary.

The on-site SAML ACS handler needs exactly one call after validating the assertion:

    grant = app.session_auth.establish(user)          # user: llmwiki.auth.User
    headers = grant.headers()                         # Set-Cookie to send with the redirect

Everything else (cookie format, expiry, CSRF token, storage) stays behind this class.
Cookie = "<sid>.<hmac>" where sid is 256 random bits; the DB keeps only sha256(sid), so a leaked
DB cannot be replayed as cookies. The session snapshot of the user (spaces!) lives for at most
`absolute_ttl`, which bounds how long AD group changes take to propagate.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..auth import User
from .webconfig import WebConfig

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    sid_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    user_json TEXT NOT NULL,
    csrf TEXT NOT NULL,
    created REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);
"""
_TOUCH_EVERY = 15.0  # seconds; avoids a DB write on every request


@dataclass(frozen=True)
class Session:
    sid_hash: str
    user: User
    csrf: str


@dataclass(frozen=True)
class SessionGrant:
    user: User
    csrf: str
    cookie: str  # complete Set-Cookie header value

    def headers(self) -> list[tuple[str, str]]:
        return [("Set-Cookie", self.cookie)]


def build_cookie(name: str, value: str, *, max_age: int, secure: bool) -> str:
    parts = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def read_cookie(header: str | None, name: str) -> str | None:
    """Return the cookie value, or None if absent or present more than once (ambiguous => reject)."""
    if not header:
        return None
    found = [p.partition("=")[2].strip() for p in header.split(";") if p.partition("=")[0].strip() == name]
    return found[0] if len(found) == 1 else None


def safe_eq(a: str, b: str) -> bool:
    """Constant-time equality that never raises on non-ASCII input (cookies arrive latin-1 decoded)."""
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


def _user_to_json(u: User) -> str:
    return json.dumps({"id": u.id, "name": u.name, "department": u.department, "part": u.part,
                       "spaces": sorted(u.spaces), "is_admin": bool(u.is_admin)}, ensure_ascii=False)


def _user_from_json(s: str) -> User:
    d = json.loads(s)
    return User(d["id"], d["name"], d["department"], d["part"], frozenset(d["spaces"]), bool(d["is_admin"]))


class SessionAuth:
    def __init__(self, cfg: WebConfig, db_path: Path | str, clock: Callable[[], float] = time.time):
        self.cfg, self._clock = cfg, clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5.0)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    @property
    def cookie_name(self) -> str:
        return "__Host-wiki_sid" if self.cfg.cookie_secure else "wiki_sid"

    def mac(self, value: str) -> str:
        return hmac.new(self.cfg.session_secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _hash(sid: str) -> str:
        return hashlib.sha256(sid.encode("utf-8")).hexdigest()

    def _sid_from_cookie(self, cookie_header: str | None) -> str | None:
        raw = read_cookie(cookie_header, self.cookie_name)
        if not raw or len(raw) > 200:
            return None
        sid, dot, tag = raw.partition(".")
        if not dot or not sid or not safe_eq(self.mac(sid), tag):
            return None
        return sid

    def establish(self, user: User) -> SessionGrant:
        """Create a new session for an authenticated user (always a fresh id: no session fixation)."""
        sid, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), self._clock()
        with self._lock:
            self._purge(now)
            self._db.execute(
                "INSERT INTO sessions (sid_hash, user_id, user_json, csrf, created, last_seen) VALUES (?,?,?,?,?,?)",
                (self._hash(sid), user.id, _user_to_json(user), csrf, now, now))
            self._db.commit()
        cookie = build_cookie(self.cookie_name, f"{sid}.{self.mac(sid)}", max_age=self.cfg.absolute_ttl,
                              secure=self.cfg.cookie_secure)
        return SessionGrant(user, csrf, cookie)

    def resolve(self, cookie_header: str | None) -> Session | None:
        sid = self._sid_from_cookie(cookie_header)
        if sid is None:
            return None
        key, now = self._hash(sid), self._clock()
        with self._lock:
            row = self._db.execute(
                "SELECT user_json, csrf, created, last_seen FROM sessions WHERE sid_hash = ?", (key,)).fetchone()
            if row is None:
                return None
            user_json, csrf, created, last_seen = row
            if now - created > self.cfg.absolute_ttl or now - last_seen > self.cfg.idle_ttl or now < created:
                self._db.execute("DELETE FROM sessions WHERE sid_hash = ?", (key,))
                self._db.commit()
                return None
            if now - last_seen > _TOUCH_EVERY:
                self._db.execute("UPDATE sessions SET last_seen = ? WHERE sid_hash = ?", (now, key))
                self._db.commit()
        try:
            return Session(key, _user_from_json(user_json), csrf)
        except (ValueError, KeyError, TypeError):
            self.terminate(cookie_header)
            return None

    def terminate(self, cookie_header: str | None) -> None:
        sid = self._sid_from_cookie(cookie_header)
        if sid is not None:
            with self._lock:
                self._db.execute("DELETE FROM sessions WHERE sid_hash = ?", (self._hash(sid),))
                self._db.commit()

    def revoke_user(self, user_id: str) -> int:
        """Invalidate every session of a user (e.g. after an AD group change). Returns the count."""
        with self._lock:
            n = self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount
            self._db.commit()
        return n

    def clear_cookie(self) -> str:
        return build_cookie(self.cookie_name, "", max_age=0, secure=self.cfg.cookie_secure)

    def _purge(self, now: float) -> None:
        self._db.execute("DELETE FROM sessions WHERE created < ? OR last_seen < ?",
                         (now - self.cfg.absolute_ttl, now - self.cfg.idle_ttl))

    def close(self) -> None:
        with self._lock:
            self._db.close()
