"""Hardening tests: DoS limits, rate limiting, log injection, input edge cases, filenames, origin policy."""
import io
import logging
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from llmwiki.auth import User
from llmwiki.web.http import HttpError
from llmwiki.web.upload import sanitize_filename
from test_web_support import make_web

TOK = "X-CSRF-Token"


def _server(w):
    from llmwiki.web.__main__ import build_server
    srv = build_server(w.env.settings, w.app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_slow_body_is_cut_off_and_part_file_removed(tmp_path):
    w = make_web(tmp_path, socket_timeout=1.0)
    c = w.client()
    c.login("ua")
    cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
    srv, port = _server(w)
    try:
        s = socket.create_connection(("127.0.0.1", port))
        s.settimeout(10)
        s.sendall((f"POST /api/upload?space=dept-a&filename=a.txt HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: {cookie}\r\n"
                   f"{TOK}: {c.csrf}\r\nContent-Length: 500\r\n\r\npartial").encode())
        t0, data = time.monotonic(), b""
        while True:  # the server must answer/close well before the 10s client timeout
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        assert time.monotonic() - t0 < 6
        assert data == b"" or b" 408 " in data.split(b"\r\n")[0]
        s.close()
    finally:
        srv.shutdown()
        srv.server_close()
    inbox = w.env.settings.data_dir / "inbox"
    assert not list(inbox.rglob("*.part")) and not list(inbox.rglob("a.txt"))


def test_idle_connection_is_dropped(tmp_path):
    w = make_web(tmp_path, socket_timeout=1.0)
    srv, port = _server(w)
    try:
        s = socket.create_connection(("127.0.0.1", port))
        s.settimeout(8)
        s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n")  # headers never finish
        t0 = time.monotonic()
        assert s.recv(100) == b""  # server closed the connection
        assert time.monotonic() - t0 < 6
    finally:
        srv.shutdown()
        srv.server_close()


def test_thread_cap_returns_503(tmp_path):
    w = make_web(tmp_path, socket_timeout=5, max_threads=2)
    srv, port = _server(w)
    try:
        hogs = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
        for h in hogs:
            h.sendall(b"GET /healthz HTTP/1.1\r\n")  # occupy both workers
        time.sleep(0.3)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3)
        assert e.value.code == 503
        for h in hogs:
            h.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_user_rate_limit_429(tmp_path):
    w = make_web(tmp_path, rate_user_per_min=5)
    c = w.client()
    c.login("ua")
    codes = [c.get("/api/me").status for _ in range(8)]
    assert codes[:4] == [200] * 4 and codes[-1] == 429
    assert c.get("/api/me").json() == {"error": "rate_limited"}
    w.clock.t += 60  # bucket refills
    assert c.get("/api/me").status == 200


def test_login_rate_limit_per_address(tmp_path):
    w = make_web(tmp_path, rate_login_per_min=3)
    c = w.client()
    assert [c.get("/api/login-info").status for _ in range(5)] == [200, 200, 200, 429, 429]
    assert c.get("/healthz").status == 200  # other routes unaffected


def test_repeated_login_failed_is_coalesced(tmp_path):
    w = make_web(tmp_path, rate_login_per_min=1000)
    c = w.client()
    tok = c.get("/api/login-info").json()["csrf"]
    for _ in range(10):
        assert c.request("POST", "/login", json_body={"user_id": "ghost"}, headers={TOK: tok}).status == 401
    rows = [r for r in w.env.audit.entries() if r[2] == "login_failed"]
    assert len(rows) == 1
    w.clock.t += 120
    c.request("POST", "/login", json_body={"user_id": "ghost"}, headers={TOK: tok})
    rows = [r for r in w.env.audit.entries() if r[2] == "login_failed"]
    assert len(rows) == 2 and "repeats_suppressed=9" in rows[1][4]


def test_read_denied_coalesced_and_strings_truncated(tmp_path):
    w = make_web(tmp_path)
    c = w.client()
    c.login("ua")
    for _ in range(5):
        c.get("/api/article", query="path=dept-b/secret.md")
    assert len([r for r in w.env.audit.entries() if r[2] == "read_denied"]) <= 1
    w.app.audit.record(User("ua", "x", "d"), "query", "q" * 5000, "d" * 5000)
    last = w.env.audit.entries()[-1]
    assert len(last[3]) == 200 and len(last[4]) == 500


def test_access_log_escapes_newlines(tmp_path, caplog):
    w = make_web(tmp_path)
    c = w.client()
    with caplog.at_level(logging.INFO, logger="llmwiki.web.access"):
        c.get("/x\nFAKE user=adm status=200")  # what a decoded %0a in PATH_INFO looks like
        c.get("/" + "a" * 500)
    msgs = [r.getMessage() for r in caplog.records]
    assert all("\n" not in m for m in msgs)
    assert "\\n" in msgs[0] and len(msgs[1]) < 300


def test_too_many_query_fields_is_400_with_headers_and_log(tmp_path, caplog):
    w = make_web(tmp_path)
    c = w.client()
    with caplog.at_level(logging.INFO, logger="llmwiki.web.access"):
        r = c.get("/healthz", query="&".join(f"a{i}=1" for i in range(50)))
    assert r.status == 400 and r.json() == {"error": "bad_request"}
    assert r.header("X-Content-Type-Options") == "nosniff" and r.header("Content-Security-Policy")
    assert any("400" in rec.getMessage() for rec in caplog.records)


def test_non_ascii_cookie_is_invalid_session_not_500(tmp_path):
    w = make_web(tmp_path)
    c = w.client()
    assert c.request("GET", "/api/me", headers={"Cookie": "wiki_sid=abc.\xe9"}, cookies=False).status == 401
    tok = c.get("/api/login-info").json()["csrf"]
    r = c.request("POST", "/login", json_body={"user_id": "ua"}, cookies=False,
                  headers={TOK: tok, "Cookie": "wiki_lcsrf=abc.\xe9"})
    assert r.status == 403
    r = c.request("POST", "/login", json_body={"user_id": "ua"}, cookies=False,
                  headers={TOK: "t\xe9", "Cookie": "wiki_lcsrf=abc.def"})
    assert r.status == 403


def test_huge_content_length_is_413(tmp_path):
    w = make_web(tmp_path)
    c = w.client()
    c.login("ua")
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/api/query", "QUERY_STRING": "", "HTTP_HOST": "wiki.test",
           "wsgi.input": io.BytesIO(b"{}"), "CONTENT_LENGTH": "9" * 5000, "CONTENT_TYPE": "application/json",
           "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in c.cookies.items()), "HTTP_X_CSRF_TOKEN": c.csrf}
    out = {}
    body = b"".join(w.app(env, lambda st, h, e=None: out.update(st=st)))
    assert out["st"].startswith("413") and b"too_large" in body


@pytest.mark.parametrize("name", ["com¹.txt", "COM².md", "lpt³.txt", "ＣＯＮ.txt", "con.txt",
                                  "nul.tar.txt", "a‮b.txt", "a​b.txt", "a#b.txt", "x☃.txt"])
def test_bad_filenames_rejected(name):
    with pytest.raises(HttpError):
        sanitize_filename(name)


def test_korean_and_plain_filenames_accepted():
    assert sanitize_filename("회의록 (최종)_v2.md") == "회의록 (최종)_v2.md"
    assert sanitize_filename("Report-1[a].txt") == "Report-1[a].txt"


def test_add_route_csrf_footgun(tmp_path):
    w = make_web(tmp_path)

    def h(app, req, s):
        return None

    with pytest.raises(ValueError):
        w.app.add_route("POST", "/x", h, auth=False)
    with pytest.raises(ValueError):
        w.app.add_route("DELETE", "/x", h, auth=False, csrf=True)
    w.app.add_route("GET", "/x", h, auth=False)
    w.app.add_route("POST", "/y", h, auth=False, self_validated=True)
    w.app.add_route("POST", "/z", h, auth=False, csrf=False)


def test_origin_policy_dev_uses_host_prod_uses_allowlist(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "p").mkdir()
    dev = make_web(tmp_path / "d")
    c = dev.client()
    c.login("ua")
    assert c.post("/logout", headers={"Origin": "http://wiki.test"}).status == 200  # same as Host
    c.login("ua")
    assert c.post("/logout", headers={"Origin": "http://evil.test"}).status == 403

    prod = make_web(tmp_path / "p", strict_origin=True, allowed_origins=frozenset({"https://wiki.example.com"}))
    c = prod.client()
    c.login("ua")
    # Host-derived "same origin" is NOT accepted in production (the proxy may rewrite Host)
    assert c.post("/logout", headers={"Origin": "http://wiki.test"}).status == 403
    assert c.post("/logout", headers={"Origin": "https://wiki.example.com.evil.test"}).status == 403
    assert c.post("/logout", headers={"Origin": "null"}).status == 403
    assert c.post("/logout", headers={"Origin": "https://wiki.example.com"}).status == 200


def test_extra_security_headers(tmp_path):
    w = make_web(tmp_path, cookie_secure=True)
    r = w.client().get("/healthz")
    assert r.header("Permissions-Policy") == "camera=(), microphone=(), geolocation=()"
    assert r.header("Strict-Transport-Security") == "max-age=31536000; includeSubDomains"
    assert r.header("Server") == "wiki"
    assert len([1 for k, _ in r.headers if k.lower() == "server"]) == 1
