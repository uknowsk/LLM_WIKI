"""Shared helpers for provider/doctor tests: loopback stub HTTP server and config writers (synthetic data only)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

PROVIDER_ENV = [
    "WIKI_LLM_PROVIDER", "WIKI_LLM_CUSTOM_CONFIG", "WIKI_LLM_API_KEY", "WIKI_LLM_API_KEY_FILE", "WIKI_LLM_BASE_URL",
    "WIKI_LLM_ALLOWED_HOSTS", "WIKI_LLM_PROXY", "WIKI_LLM_CA_BUNDLE", "WIKI_LLM_MODEL", "WIKI_EMBED_PROVIDER",
    "WIKI_EMBED_CUSTOM_CONFIG", "WIKI_EMBED_API_KEY", "WIKI_EMBED_API_KEY_FILE", "WIKI_EMBED_BASE_URL",
    "WIKI_EMBED_ALLOWED_HOSTS", "WIKI_EMBED_PROXY", "WIKI_EMBED_CA_BUNDLE", "WIKI_EMBED_MODEL",
    "WIKI_LLM_CONTEXT_TOKENS", "WIKI_ENV", "WIKI_SESSION_SECRET", "WIKI_ALLOWED_ORIGINS", "WIKI_DATA_DIR",
    "WIKI_AUTH_PROVIDER",
]


@pytest.fixture(autouse=True)
def clean_provider_env(monkeypatch):
    for k in PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)


class Stub:
    """Loopback server. `respond(handler, path, headers, body)` returns (status, headers, bytes|list[bytes])."""

    def __init__(self, respond):
        self.seen: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _go(self):
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                rec = {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body,
                       "raw": raw, "method": self.command}
                outer.seen.append(rec)
                status, headers, payload = respond(rec)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                for chunk in ([payload] if isinstance(payload, bytes) else payload):
                    self.wfile.write(chunk)
                    self.wfile.flush()

            do_POST = do_PUT = do_GET = _go

            def log_message(self, *a):
                pass

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, request, client_address):  # clients that time out/abort are part of the tests
                pass

        self.srv = Quiet(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def json_reply(obj, status=200):
    return status, {"Content-Type": "application/json"}, json.dumps(obj).encode("utf-8")


def sse_reply(events: list, done: bool = True):
    chunks = [("data: " + (e if isinstance(e, str) else json.dumps(e)) + "\n\n").encode("utf-8") for e in events]
    if done:
        chunks.append(b"data: [DONE]\n\n")
    return 200, {"Content-Type": "text/event-stream"}, chunks


@pytest.fixture()
def serve():
    servers: list[Stub] = []

    def make(respond) -> Stub:
        s = Stub(respond)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def write_cfg(tmp_path, base, **over):
    cfg = {
        "url": base + "/chat",
        "headers": {"Authorization": "Bearer {api_key}", "X-Request-Id": "{uuid}"},
        "body": {"model": "{model}", "messages": [{"role": "system", "content": "{system}"},
                                                   {"role": "user", "content": "{prompt}"}],
                 "temperature": "{temperature}", "max_tokens": "{max_tokens}"},
        "response_path": "choices.0.message.content", "error_path": "error.message",
        "context_exceeded_patterns": ["context", "token limit"], "timeout": 5,
    }
    cfg.update(over)
    cfg = {k: v for k, v in cfg.items() if v is not None}
    p = tmp_path / "llm.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


OK_REPLY = {"choices": [{"message": {"content": "안녕 OK"}}]}
