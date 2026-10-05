"""Local-only guard in front of the WSGI app. Every request must (1) come from a loopback peer, (2) carry a Host header
naming this exact loopback origin (DNS-rebinding defense) and (3) hold a valid session OR present the per-launch token,
which is exchanged ONCE (GET / only) for a session cookie and a redirect to a token-less URL. Rejections are generic and never echo input."""
from __future__ import annotations

import ipaddress
import threading

from ..web.http import Request, Response, error_response
from .identity import PersonalAuthProvider


def is_loopback_addr(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address((raw or "").strip().split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def loopback_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})


def loopback_origins(port: int) -> frozenset[str]:
    return frozenset("http://" + h for h in loopback_hosts(port))


class LocalOnlyGuard:
    def __init__(self, app, provider: PersonalAuthProvider, port: int):
        self.app, self.provider, self.port = app, provider, port
        self._hosts = loopback_hosts(port)
        self._used, self._lock = False, threading.Lock()  # the launch token works exactly once

    def __getattr__(self, name):  # cfg, add_route, ... of the wrapped app
        return getattr(self.app, name)

    def _reply(self, resp: Response, start_response, head: bool = False):
        self.app._harden(resp)
        start_response(resp.status_line, resp.headers)
        return [b"" if head else resp.body]

    def __call__(self, environ, start_response):
        head = str(environ.get("REQUEST_METHOD", "")).upper() == "HEAD"
        if not is_loopback_addr(str(environ.get("REMOTE_ADDR", ""))):
            return self._reply(error_response(403, "forbidden"), start_response, head)
        if str(environ.get("HTTP_HOST", "")).strip().lower() not in self._hosts:
            return self._reply(error_response(403, "forbidden"), start_response, head)
        req = Request(environ)
        if self.app.session_auth.resolve(req.header("Cookie")) is not None:
            return self.app(environ, start_response)
        token = req.query.get("token")
        if req.method == "GET" and req.path == "/" and token and not req.bad_query:
            user = self.provider.authenticate({"launch_token": token})  # constant-time, also after the token was used
            with self._lock:
                fresh, self._used = user is not None and not self._used, self._used or user is not None
            if fresh:
                grant = self.app.start_session(req, user)
                resp = Response(302, b"", "text/plain; charset=utf-8", grant.headers() + [("Location", "/")])
                return self._reply(resp, start_response, head)
        return self._reply(error_response(401, "unauthorized"), start_response, head)
