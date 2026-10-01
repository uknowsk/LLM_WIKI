"""Helpers for web tests: a tiny cookie-keeping WSGI client (direct calls, no sockets)."""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

from llmwiki.auth import DevAuthProvider, User
from llmwiki.engine.llm import FakeLLM
from llmwiki.web import create_app
from llmwiki.web.webconfig import WebConfig
from test_engine_support import UA, UB, make_env

ADMIN = User("adm", "Admin", "it", None, frozenset({"dept-a"}), True)
SECRET = "S" * 40


@dataclass
class Resp:
    status: int
    headers: list
    body: bytes

    def header(self, name: str):
        vals = [v for k, v in self.headers if k.lower() == name.lower()]
        return vals[0] if vals else None

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Client:
    def __init__(self, app):
        self.app, self.cookies, self.csrf = app, {}, None

    def request(self, method, path, *, query="", body=None, json_body=None, headers=None, ctype=None, cookies=True):
        if json_body is not None:
            body, ctype = json.dumps(json_body).encode("utf-8"), "application/json"
        body = body if body is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "SERVER_NAME": "wiki.test",
               "SERVER_PORT": "80", "HTTP_HOST": "wiki.test", "wsgi.url_scheme": "http", "wsgi.input": io.BytesIO(body),
               "CONTENT_LENGTH": str(len(body)) if method == "POST" else ""}
        if ctype:
            env["CONTENT_TYPE"] = ctype
        if cookies and self.cookies:
            env["HTTP_COOKIE"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        out = {}
        chunks = self.app(env, lambda status, hdrs, exc=None: out.update(status=int(status.split()[0]), headers=hdrs))
        resp = Resp(out["status"], out["headers"], b"".join(chunks))
        for k, v in resp.headers:
            if k.lower() == "set-cookie":
                name, _, rest = v.partition("=")
                val = rest.split(";")[0]
                if "Max-Age=0" in v:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = val
        return resp

    def get(self, path, query="", **kw):
        return self.request("GET", path, query=query, **kw)

    def post(self, path, json_body=None, csrf=True, **kw):
        h = dict(kw.pop("headers", {}) or {})
        if csrf and self.csrf:
            h.setdefault("X-CSRF-Token", self.csrf)
        return self.request("POST", path, json_body=json_body, headers=h, **kw)

    def login(self, uid):
        tok = self.get("/api/login-info").json()["csrf"]
        r = self.request("POST", "/login", json_body={"user_id": uid}, headers={"X-CSRF-Token": tok})
        if r.status == 200:
            self.csrf = r.json()["csrf"]
        return r


@dataclass
class WebEnv:
    env: object
    app: object
    clock: Clock
    cfg: WebConfig
    llm: FakeLLM

    def client(self):
        return Client(self.app)


def make_web(tmp_path: Path, llm=None, **cfg_kw) -> WebEnv:
    env = make_env(tmp_path / "data")
    clock, llm = Clock(), llm or FakeLLM()
    kw = dict(session_secret=SECRET.encode(), cookie_secure=False, max_upload_bytes=1000)
    kw.update(cfg_kw)
    cfg = WebConfig(**kw)
    provider = DevAuthProvider({u.id: u for u in (UA, UB, ADMIN)})
    app = create_app(env.settings, llm, env.store, env.audit, provider, config=cfg, clock=clock)
    return WebEnv(env, app, clock, cfg, llm)
