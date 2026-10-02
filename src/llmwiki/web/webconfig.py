"""Web-layer settings from the environment. Fails closed in production."""
from __future__ import annotations

import ipaddress
import os
import secrets
from dataclasses import dataclass

from ..config import Settings

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
MIN_SECRET_LEN = 32


def is_dev_env(settings: Settings) -> bool:
    return str(settings.env).strip().lower() in ("development", "test")


@dataclass(frozen=True)
class WebConfig:
    session_secret: bytes
    cookie_secure: bool
    idle_ttl: int = 30 * 60  # seconds without a request before the session dies
    absolute_ttl: int = 8 * 60 * 60  # hard lifetime: AD group changes propagate by re-login
    max_upload_bytes: int = 50 * 1024 * 1024
    max_json_bytes: int = 64 * 1024
    allowed_origins: frozenset[str] = frozenset()  # extra Origins accepted (reverse proxy hostnames)
    host: str = "127.0.0.1"
    port: int = 8080
    strict_origin: bool = False  # production: Origin must be listed in allowed_origins (Host header is not trusted)
    socket_timeout: float = 30.0
    max_threads: int = 64
    rate_user_per_min: int = 120
    rate_login_per_min: int = 20
    trusted_proxies: frozenset[str] = frozenset()  # exact proxy IPs whose X-Forwarded-For is honoured


def _int(e, key: str, default: int) -> int:
    raw = e.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = int(str(raw).strip())
    except ValueError:
        raise RuntimeError(f"{key} must be an integer") from None
    if v <= 0:
        raise RuntimeError(f"{key} must be positive")
    return v


def _bool(e, key: str, default: bool) -> bool:
    raw = e.get(key)
    if raw is None or not str(raw).strip():
        return default
    v = str(raw).strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise RuntimeError(f"{key} must be a boolean")


def _proxies(e) -> frozenset[str]:
    out = set()
    for item in (e.get("WIKI_TRUSTED_PROXIES") or "").split(","):
        if item.strip():
            try:
                out.add(str(ipaddress.ip_address(item.strip())))
            except ValueError:
                raise RuntimeError("WIKI_TRUSTED_PROXIES must be a comma list of exact IP addresses") from None
    return frozenset(out)


def load_web_config(settings: Settings, environ: dict[str, str] | None = None) -> WebConfig:
    """Production (anything but development/test) refuses to start without WIKI_SESSION_SECRET (>= 32 chars).
    Development generates a random per-process secret (sessions do not survive a restart)."""
    e = os.environ if environ is None else environ
    dev = is_dev_env(settings)
    secret = (e.get("WIKI_SESSION_SECRET") or "").strip()
    if not secret:
        if not dev:
            raise RuntimeError("WIKI_SESSION_SECRET is required when WIKI_ENV is not 'development' or 'test'")
        secret = secrets.token_urlsafe(48)
    elif len(secret) < MIN_SECRET_LEN and not dev:
        raise RuntimeError(f"WIKI_SESSION_SECRET must be at least {MIN_SECRET_LEN} characters")
    origins = frozenset(o.strip().rstrip("/").lower() for o in (e.get("WIKI_ALLOWED_ORIGINS") or "").split(",") if o.strip())
    if not dev and not origins:
        raise RuntimeError("WIKI_ALLOWED_ORIGINS is required when WIKI_ENV is not 'development' or 'test': set it to the "
                           "exact browser origin(s) of the site, comma-separated, e.g. "
                           "WIKI_ALLOWED_ORIGINS=https://wiki.example.com (scheme://host[:port], no path)")
    return WebConfig(
        session_secret=secret.encode("utf-8"),
        cookie_secure=_bool(e, "WIKI_COOKIE_SECURE", not dev),
        idle_ttl=_int(e, "WIKI_SESSION_IDLE_SECONDS", 30 * 60),
        absolute_ttl=_int(e, "WIKI_SESSION_ABSOLUTE_SECONDS", 8 * 60 * 60),
        max_upload_bytes=_int(e, "WIKI_MAX_UPLOAD_BYTES", 50 * 1024 * 1024),
        max_json_bytes=_int(e, "WIKI_MAX_JSON_BYTES", 64 * 1024),
        allowed_origins=origins,
        host=(e.get("WIKI_HOST") or "127.0.0.1").strip(),
        port=_int(e, "WIKI_PORT", 8080),
        strict_origin=not dev,
        socket_timeout=float(_int(e, "WIKI_SOCKET_TIMEOUT", 30)),
        max_threads=_int(e, "WIKI_MAX_THREADS", 64),
        rate_user_per_min=_int(e, "WIKI_RATE_USER_PER_MIN", 120),
        rate_login_per_min=_int(e, "WIKI_RATE_LOGIN_PER_MIN", 20),
        trusted_proxies=_proxies(e),
    )
