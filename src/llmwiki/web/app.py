"""WSGI application: routing, session + CSRF enforcement, security headers, error shaping."""
from __future__ import annotations

import ipaddress
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..audit import AuditLog
from ..auth import AuthProvider, DevAuthProvider, User
from ..config import Settings
from ..engine.embed import Embedder
from ..engine.llm import LLMClient
from ..engine.store import Store
from . import api, ui
from .http import HttpError, Request, Response, error_response, json_response
from .limits import AuditGuard, RateLimiter
from .sessions import SessionAuth, SessionGrant, build_cookie, read_cookie, safe_eq
from .capture import handle_capture
from .feedback import handle_feedback
from .upload import handle_upload
from .webconfig import WebConfig, is_dev_env, load_web_config

access_log = logging.getLogger("llmwiki.web.access")
log = logging.getLogger("llmwiki.web")
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_LOGIN_PATHS = ("/login", "/api/login-info")
_LOGIN_TOKEN_TTL = 600
_ANON = User("-", "-", "-")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _norm_ip(raw: str) -> str | None:
    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        return None


def client_addr(environ: dict, trusted: frozenset[str]) -> str:
    """REMOTE_ADDR, unless it is a configured trusted proxy: then the right-most X-Forwarded-For entry that is
    not itself a trusted proxy. Anything unparsable falls back to the peer address (never trust the left side)."""
    peer = str(environ.get("REMOTE_ADDR", "-"))
    if not trusted or _norm_ip(peer) not in trusted:
        return peer
    for entry in reversed(str(environ.get("HTTP_X_FORWARDED_FOR", "")).split(",")):
        ip = _norm_ip(entry)
        if ip is None:
            return peer
        if ip not in trusted:
            return ip
    return peer


@dataclass(frozen=True)
class Route:
    handler: Callable
    auth: bool = True  # requires a valid session
    csrf: bool = True  # state-changing requests need X-CSRF-Token == session token (only if auth)


class WikiApp:
    def __init__(self, settings: Settings, llm: LLMClient, store: Store, audit: AuditLog,
                 auth_provider: AuthProvider, cfg: WebConfig, clock: Callable[[], float] = time.time,
                 embedder: Embedder | None = None):
        if isinstance(auth_provider, DevAuthProvider) and not is_dev_env(settings):
            raise RuntimeError("Dev auth provider is allowed only when WIKI_ENV is 'development' or 'test'")
        if getattr(auth_provider, "personal_only", False) and not cfg.personal:
            raise RuntimeError("The personal auth provider is only available through `python -m llmwiki.personal`")
        self.settings, self.llm, self.store, self.embedder = settings, llm, store, embedder
        self.audit = AuditGuard(audit, clock)  # truncates + coalesces repeated denials
        self._limiter = RateLimiter(clock)
        self.auth_provider, self.cfg = auth_provider, cfg
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.session_auth = SessionAuth(cfg, settings.data_dir / "sessions.db", clock)
        self._routes: dict[tuple[str, str], Route] = {}
        for method, path, handler, auth in (
            ("GET", "/healthz", api.healthz, False), ("GET", "/api/me", api.me, True),
            ("POST", "/api/query", api.query, True), ("GET", "/api/article", api.article, True),
            ("POST", "/api/upload", handle_upload, True), ("POST", "/api/capture", handle_capture, True),
            ("POST", "/api/feedback", handle_feedback, True),
            ("GET", "/api/audit", api.audit_entries, True),
            ("GET", "/api/login-info", self._login_info, False), ("POST", "/login", self._login, False),
            ("POST", "/logout", self._logout, True),
            ("GET", "/", ui.page, False), ("GET", "/static/app.js", ui.script, False),
            ("GET", "/static/app.css", ui.style, False),
        ):
            self.add_route(method, path, handler, auth=auth, self_validated=(path == "/login"))

    # ---- extension points for the on-site SAML integration -------------------------------------
    def add_route(self, method: str, path: str, handler: Callable, *, auth: bool = True, csrf: bool = True,
                  self_validated: bool = False) -> None:
        """handler(app, request, session_or_None) -> Response.

        The CSRF header is only enforced for authenticated (session) routes. Registering an unsafe
        method (POST/PUT/PATCH/DELETE) with auth=False and csrf=True would silently skip CSRF, so it
        raises ValueError unless self_validated=True: the handler then takes full responsibility.
        The SAML ACS handler must pass self_validated=True (e.g. add_route("POST", "/saml/acs", h,
        auth=False, csrf=False, self_validated=True)) and itself verify the assertion signature,
        InResponseTo, RelayState, the NotBefore/NotOnOrAfter time window and replay (assertion id
        seen before). The IdP posts cross-site, so the Origin check still applies unless the IdP
        origin is listed in WIKI_ALLOWED_ORIGINS."""
        method = method.upper()
        if method not in _SAFE_METHODS and not auth and csrf and not self_validated:
            raise ValueError(f"{method} {path}: csrf=True is ignored when auth=False; pass self_validated=True "
                             "if the handler performs its own request validation")
        self._routes[(method, path)] = Route(handler, auth, csrf and not self_validated)

    def start_session(self, req: Request, user: User) -> SessionGrant:
        """Turn an authenticated User into a session (used by /login and by the SAML ACS handler)."""
        self.session_auth.terminate(req.header("Cookie"))  # drop any pre-existing session (fixation)
        grant = self.session_auth.establish(user)
        self.audit.record(user, "login", "-")
        return grant

    # ---- WSGI ------------------------------------------------------------------------------------
    def __call__(self, environ, start_response):
        t0, req, uid = time.monotonic(), Request(environ), "-"
        try:
            if req.bad_query:
                raise HttpError(400, "bad_request")
            resp, uid = self._dispatch(req)
        except HttpError as e:
            resp = error_response(e.status, e.code)
        except Exception as e:  # never leak details; the type name is enough for the operator
            log.error("unhandled %s on %s %s", type(e).__name__, req.method, req.path)
            resp = error_response(500, "internal")
        self._harden(resp)
        access_log.info("%r %r %d user=%s %.0fms", req.method[:16], req.path[:200], resp.status, uid,
                        (time.monotonic() - t0) * 1000)  # repr: no log injection; no query string, no bodies
        start_response(resp.status_line, resp.headers)
        return [b"" if req.method == "HEAD" else resp.body]

    def _dispatch(self, req: Request) -> tuple[Response, str]:
        route = self._routes.get((req.method, req.path))
        if route is None:
            known = any(p == req.path for _, p in self._routes)
            raise HttpError(405 if known else 404, "method_not_allowed" if known else "not_found")
        if req.path in _LOGIN_PATHS and not self._limiter.allow(
                "addr:" + client_addr(req.environ, self.cfg.trusted_proxies), self.cfg.rate_login_per_min):
            raise HttpError(429, "rate_limited")
        unsafe = req.method not in _SAFE_METHODS
        if unsafe:
            self._check_origin(req)
        session = None
        if route.auth:
            session = self.session_auth.resolve(req.header("Cookie"))
            if session is None:
                raise HttpError(401, "unauthorized")
            if not self._limiter.allow("user:" + session.user.id, self.cfg.rate_user_per_min):
                raise HttpError(429, "rate_limited")
            if unsafe and route.csrf:
                sent = req.header("X-CSRF-Token") or ""
                if not sent or not safe_eq(sent, session.csrf):
                    raise HttpError(403, "csrf")
        resp = route.handler(self, req, session)
        return resp, (session.user.id if session else "-")

    def _check_origin(self, req: Request) -> None:
        origin = req.header("Origin")
        if origin is None:
            return
        if origin.rstrip("/").lower() in {o.lower() for o in self.cfg.allowed_origins}:
            return
        if self.cfg.strict_origin:  # production: the Host header may be rewritten by the proxy; never trust it
            raise HttpError(403, "csrf")
        host = req.header("Host") or ""
        try:
            same = bool(host) and urlsplit(origin).netloc.lower() == host.lower()
        except ValueError:
            same = False
        if not same:
            raise HttpError(403, "csrf")

    def _harden(self, resp: Response) -> None:
        names = {k.lower() for k, _ in resp.headers}
        resp.headers[:] = [(k, v) for k, v in resp.headers if k.lower() != "server"]
        extra = {"Server": "wiki", "Permissions-Policy": "camera=(), microphone=(), geolocation=()", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                 "Referrer-Policy": "no-referrer", "Content-Security-Policy": CSP,
                 "Cross-Origin-Resource-Policy": "same-origin"}
        if self.cfg.cookie_secure:
            extra["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        for k, v in extra.items():
            if k.lower() not in names:
                resp.headers.append((k, v))

    # ---- login / logout --------------------------------------------------------------------------
    @property
    def _lcsrf_name(self) -> str:
        return "__Host-wiki_lcsrf" if self.cfg.cookie_secure else "wiki_lcsrf"

    def _login_token(self, req: Request) -> str | None:
        raw = read_cookie(req.header("Cookie"), self._lcsrf_name)
        tok, dot, tag = (raw or "").partition(".")
        if not raw or not dot or not tok or not safe_eq(self.session_auth.mac("login:" + tok), tag):
            return None
        return tok

    def _login_info(self, app, req: Request, session) -> Response:
        """Public. Issues the pre-login CSRF token (double-submit, HMAC-bound). In dev only, lists user ids."""
        tok = secrets.token_urlsafe(32)
        cookie = build_cookie(self._lcsrf_name, f"{tok}.{self.session_auth.mac('login:' + tok)}",
                              max_age=_LOGIN_TOKEN_TTL, secure=self.cfg.cookie_secure)
        body: dict = {"csrf": tok, "provider": self.settings.auth_provider}
        if is_dev_env(self.settings) and isinstance(self.auth_provider, DevAuthProvider):
            body["dev_users"] = sorted(str(k) for k in getattr(self.auth_provider, "_users", {}))
        return json_response(200, body, [("Set-Cookie", cookie)])

    def _login(self, app, req: Request, session) -> Response:
        tok = self._login_token(req)
        sent = req.header("X-CSRF-Token") or ""
        if tok is None or not sent or not safe_eq(sent, tok):
            raise HttpError(403, "csrf")
        creds = req.read_json(self.cfg.max_json_bytes)
        if len(creds) > 8 or not all(isinstance(v, str) and len(v) <= 256 for v in creds.values()):
            raise HttpError(400, "bad_request")
        try:
            user = self.auth_provider.authenticate(creds)
        except Exception:
            log.error("auth provider failure")
            user = None
        if user is None:
            self.audit.record(_ANON, "login_failed", str(creds.get("user_id", ""))[:64])
            raise HttpError(401, "invalid_credentials")
        grant = self.start_session(req, user)
        return json_response(200, api.user_payload(user, grant.csrf),
                             grant.headers() + [("Set-Cookie", build_cookie(self._lcsrf_name, "", max_age=0,
                                                                            secure=self.cfg.cookie_secure))])

    def _logout(self, app, req: Request, session) -> Response:
        self.session_auth.terminate(req.header("Cookie"))
        self.audit.record(session.user, "logout", "-")
        return json_response(200, {"ok": True}, [("Set-Cookie", self.session_auth.clear_cookie())])


def create_app(settings: Settings, llm: LLMClient, store: Store, audit: AuditLog, auth_provider: AuthProvider,
               *, config: WebConfig | None = None, environ: dict[str, str] | None = None,
               clock: Callable[[], float] = time.time, embedder: Embedder | None = None) -> WikiApp:
    """Build the WSGI callable. Raises RuntimeError in production without WIKI_SESSION_SECRET."""
    return WikiApp(settings, llm, store, audit, auth_provider, config or load_web_config(settings, environ), clock,
                   embedder=embedder)
