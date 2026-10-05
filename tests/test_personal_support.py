"""Helpers for personal-mode tests: a runtime on a tmp home with fake model clients and a loopback WSGI client."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from llmwiki.engine.embed import FakeEmbedder
from llmwiki.engine.llm import FakeLLM
from llmwiki.personal.runtime import Runtime, build_runtime
from test_pipeline_support import responder
from test_web_support import Client

PORT = 45678
TOKEN = "T" * 32


def personal_responder(system: str, prompt: str) -> str:
    if "QUESTION:" in prompt:
        return "답변입니다 [1]"
    return responder(system, prompt)


class RemoteApp:
    """Sets REMOTE_ADDR like a real server would."""

    def __init__(self, app, addr: str = "127.0.0.1"):
        self.app, self.addr = app, addr

    def __call__(self, environ, start_response):
        environ["REMOTE_ADDR"] = self.addr
        return self.app(environ, start_response)


class PClient(Client):
    def __init__(self, app, port: int = PORT, host: str | None = None, addr: str = "127.0.0.1"):
        super().__init__(RemoteApp(app, addr))
        self.default_host = host if host is not None else f"127.0.0.1:{port}"

    def request(self, method, path, *, headers=None, **kw):
        h = dict(headers or {})
        if self.default_host:
            h.setdefault("Host", self.default_host)
        return super().request(method, path, headers=h, **kw)

    def enter(self, token: str = TOKEN):
        r = self.get("/", query=f"token={token}")
        if r.status == 302:
            self.csrf = self.get("/api/me").json()["csrf"]
        return r


@dataclass
class P:
    rt: Runtime
    llm: FakeLLM
    home: Path

    def client(self, **kw) -> PClient:
        return PClient(self.rt.wsgi, **kw)

    def drop(self, rel: str, data: bytes | str) -> Path:
        p = self.home / "inbox" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return p

    def settle(self) -> None:
        """Two scans (the stability check needs an unchanged signature), then run the queue."""
        self.rt.worker.min_age = 0.0
        self.rt.worker.scan_once()
        self.rt.worker.run_once()


def make_personal(tmp_path: Path, extra_env: dict | None = None, llm: FakeLLM | None = None, port: int = PORT,
                  **kw) -> P:
    home = tmp_path / "LLMWiki"
    env = {"WIKI_LLM_BASE_URL": "http://127.0.0.1:1/v1", "WIKI_PERSONAL_HOME": str(home)}
    env.update(extra_env or {})
    llm = llm or FakeLLM(personal_responder)
    rt = build_runtime(env, home, port, llm=llm, embedder=FakeEmbedder(), token=TOKEN, sleep=lambda s: None, **kw)
    return P(rt, llm, home)
