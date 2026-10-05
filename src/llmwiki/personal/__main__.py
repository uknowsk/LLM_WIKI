"""python -m llmwiki.personal [--no-browser] [--port N] [--check]: web UI + ingest worker in ONE process, loopback only."""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import webbrowser
from collections.abc import Mapping
from wsgiref.simple_server import make_server

from ..doctor import report
from ..web.__main__ import _QuietHandler, _ThreadingServer
from .check import run_check
from .runtime import build_runtime
from .settings import PersonalConfigError, personal_environ, personal_home, validate_bind_host


class _Trampoline:
    """The server needs an app before the port (and thus the real app) is known; requests only arrive after set()."""

    def __init__(self):
        self.app = None

    def __call__(self, environ, start_response):
        if self.app is None:
            start_response("503 Service Unavailable", [("Content-Type", "text/plain")])
            return [b"starting"]
        return self.app(environ, start_response)


def make_local_server(host: str, port: int, app):
    """wsgiref server bound to exactly `host` (validated loopback), thread per connection with a hard cap."""
    validate_bind_host(host)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    # No SO_REUSEADDR (on Windows it lets another local process bind the same port and receive our token/cookies);
    # SO_EXCLUSIVEADDRUSE additionally stops anyone from binding it later.
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        _ThreadingServer.server_bind(self)

    server_cls = type("_Server", (_ThreadingServer,), {"max_threads": 16, "address_family": family,
                                                       "allow_reuse_address": False, "server_bind": server_bind})
    handler_cls = type("_Handler", (_QuietHandler,), {"timeout": 30.0})
    return make_server(host, port, app, server_class=server_cls, handler_class=handler_cls)


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmwiki.personal", description="개인 PC용 LLM 위키 (이 PC에서만 접속 가능)")
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않고 주소만 출력")
    ap.add_argument("--port", type=int, default=0, help="포트 (기본 0 = 빈 포트 자동 선택)")
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 또는 ::1 만 허용 (WIKI_HOST 는 무시)")
    ap.add_argument("--check", action="store_true", help="환경 점검만 하고 종료 (FAIL 이 있으면 종료 코드 1)")
    ap.add_argument("--probe-context", action="store_true", help="--check 와 함께: 컨텍스트 한계를 실측")
    args = ap.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        host = validate_bind_host(args.host)
        if not 0 <= args.port <= 65535:
            raise PersonalConfigError("port must be 0..65535")
    except PersonalConfigError as exc:
        print(f"llmwiki.personal: {exc}", file=sys.stderr)
        return 2
    if args.check:
        results, code = run_check(env, host, args.probe_context)
        print(report.as_text(results))
        return code
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("llmwiki.web.access").setLevel(logging.WARNING)  # the UI polls the status every 5 s: no log spam
    home = personal_home(personal_environ(env))
    tramp = _Trampoline()
    try:
        server = make_local_server(host, args.port, tramp)
    except OSError as exc:
        print(f"llmwiki.personal: cannot listen on {host}:{args.port} ({type(exc).__name__}); try --port 0", file=sys.stderr)
        return 2
    port = server.server_port
    try:
        rt = build_runtime(env, home, port)
    except (PersonalConfigError, RuntimeError, ValueError) as exc:
        server.server_close()
        print(f"llmwiki.personal: startup refused: {exc}", file=sys.stderr)
        return 2
    tramp.app = rt.wsgi
    url = rt.launch_url(host, port)
    print(f"개인 위키가 시작되었습니다. 데이터 폴더: {home}", flush=True)
    print(f"이 주소로 접속하세요 (한 번만 사용 가능, 다른 사람에게 보내지 마세요):\n  {url}", flush=True)
    print("이 주소는 한 번만 쓸 수 있고 다시 표시되지 않습니다. 브라우저가 열리지 않았다면 프로그램을 다시 시작해 새 주소를 받으세요.", flush=True)
    print("종료: Ctrl+C", flush=True)
    rt.worker.start()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        rt.close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    raise SystemExit(main())
