"""Dev/pilot server: python -m llmwiki.web  (wsgiref + threads; binds 127.0.0.1 unless WIKI_HOST is set).

Dev users (WIKI_AUTH_PROVIDER=dev, WIKI_ENV=development only) come from the JSON file WIKI_DEV_USERS_FILE:
[{"id": "ua", "name": "Kim", "department": "dept-a", "part": null, "spaces": ["dept-a"], "is_admin": false}]
"""
from __future__ import annotations

import json
import os
import threading
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from ..audit import AuditLog
from ..auth import User, get_provider
from ..config import Settings, load_settings
from ..engine.embed import embedder_from_env
from ..engine.llm import OpenAICompatClient
from ..engine.store import Store
from .app import WikiApp, create_app
from .webconfig import load_web_config


_BUSY = (b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: 17\r\n"
         b"Retry-After: 1\r\nConnection: close\r\n\r\n" + b'{"error":"busy"}\n')


class _ThreadingServer(ThreadingMixIn, WSGIServer):
    """Thread per connection, but at most `max_threads` at once; beyond that: immediate 503."""
    daemon_threads = True
    max_threads = 64

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(self.max_threads)
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1.0)
                request.sendall(_BUSY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):  # access logging is done by the app (no query strings)
        pass


def load_dev_users(path: str | None) -> dict[str, User]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    return {r["id"]: User(r["id"], r["name"], r["department"], r.get("part"),
                          frozenset(r.get("spaces", [])), bool(r.get("is_admin", False))) for r in rows}


def build_server(settings: Settings, app: WikiApp, host: str, port: int):
    cfg = getattr(app, "cfg", None)
    timeout, threads = getattr(cfg, "socket_timeout", 30.0), getattr(cfg, "max_threads", 64)
    server_cls = type("_Server", (_ThreadingServer,), {"max_threads": threads})
    handler_cls = type("_Handler", (_QuietHandler,), {"timeout": timeout})  # StreamRequestHandler applies it
    return make_server(host, port, app, server_class=server_cls, handler_class=handler_cls)


def main() -> None:
    import logging
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    cfg = load_web_config(settings)  # raises in production without WIKI_SESSION_SECRET
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    provider = get_provider(settings, load_dev_users(os.environ.get("WIKI_DEV_USERS_FILE")))
    app = create_app(settings, OpenAICompatClient.from_settings(settings), Store(settings.db_path),
                     AuditLog(settings.db_path), provider, config=cfg, embedder=embedder_from_env(settings))
    server = build_server(settings, app, cfg.host, cfg.port)
    logging.getLogger("llmwiki.web").info("listening on http://%s:%d", cfg.host, cfg.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
