"""Local-only hardening: loopback peer, Host header, launch token, Origin/CSRF, cookie flags, central refusal."""
import http.client
import io
import logging
import threading

import pytest

from llmwiki.auth import get_provider
from llmwiki.config import Settings as CfgSettings
from llmwiki.personal.__main__ import main as personal_main, make_local_server
from llmwiki.personal.identity import PersonalAuthProvider
from llmwiki.personal.settings import PersonalConfigError, validate_bind_host
from llmwiki.web import __main__ as web_main
from llmwiki.web import create_app
from llmwiki.web.webconfig import WebConfig
from test_personal_support import PORT, TOKEN, make_personal
from test_web_support import make_web


def test_rejects_non_loopback_peer_even_with_valid_token(tmp_path):
    p = make_personal(tmp_path)
    for addr in ("192.168.0.5", "10.1.2.3", "8.8.8.8", "::2", ""):
        r = p.client(addr=addr).get("/", query=f"token={TOKEN}")
        assert r.status == 403 and r.json() == {"error": "forbidden"}


def test_accepts_ipv6_and_mapped_loopback_peers(tmp_path):
    for i, addr in enumerate(("127.0.0.1", "::1", "::ffff:127.0.0.1", "127.0.0.9")):
        p = make_personal(tmp_path / str(i))  # the token is single-use: one runtime per address
        assert p.client(addr=addr).get("/", query=f"token={TOKEN}").status == 302


@pytest.mark.parametrize("host", ["evil.example", "evil.example:45678", "127.0.0.1", "127.0.0.1:1", "localhost",
                                  "127.0.0.1.evil.example:45678", "0.0.0.0:45678", "[::1]", "", "127.0.0.1:45678, evil"])
def test_rejects_bad_host_header(tmp_path, host):
    p = make_personal(tmp_path)
    c = p.client(host=host)
    r = c.get("/", query=f"token={TOKEN}")
    assert r.status == 403 and r.json() == {"error": "forbidden"}  # same generic body as a non-loopback peer


@pytest.mark.parametrize("host", [f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}", f"LOCALHOST:{PORT}"])
def test_accepts_loopback_host_names(tmp_path, host):
    p = make_personal(tmp_path)
    assert p.client(host=host).get("/", query=f"token={TOKEN}").status == 302


def test_missing_host_header_is_refused(tmp_path):
    p = make_personal(tmp_path)
    out = {}
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "QUERY_STRING": f"token={TOKEN}", "REMOTE_ADDR": "127.0.0.1",
           "wsgi.input": io.BytesIO(b"")}
    body = b"".join(p.rt.wsgi(env, lambda status, hdrs, exc=None: out.update(status=status)))
    assert out["status"].startswith("403") and b"forbidden" in body


def test_no_token_and_no_session_is_401(tmp_path):
    p = make_personal(tmp_path)
    c = p.client()
    for path in ("/", "/api/me", "/healthz", "/static/app.js", "/api/login-info"):
        r = c.get(path)
        assert r.status == 401 and r.json() == {"error": "unauthorized"}, path
    assert c.request("POST", "/login", json_body={"user_id": "me"}).status == 401


@pytest.mark.parametrize("token", ["", "wrong", "T" * 31, "T" * 33, TOKEN.lower(), "한" * 32])
def test_wrong_token_is_401_and_sets_no_cookie(tmp_path, token):
    p = make_personal(tmp_path)
    c = p.client()
    r = c.get("/", query="token=" + token)
    assert r.status == 401 and r.header("Set-Cookie") is None and not c.cookies


def test_token_only_via_get(tmp_path):
    p = make_personal(tmp_path)
    r = p.client().request("POST", "/api/query", query=f"token={TOKEN}", json_body={"question": "x"})
    assert r.status == 401


def test_token_exchange_sets_strict_httponly_cookie_and_redirects_without_token(tmp_path):
    p = make_personal(tmp_path)
    c = p.client()
    r = c.get("/", query=f"token={TOKEN}")
    assert r.status == 302 and r.header("Location") == "/"
    cookie = r.header("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and TOKEN not in cookie
    assert r.header("Referrer-Policy") == "no-referrer" and r.header("Cache-Control") == "no-store"
    assert "default-src 'self'" in r.header("Content-Security-Policy")
    assert c.get("/").status == 200
    me = c.get("/api/me").json()
    assert me["id"] == "me" and me["spaces"] == ["personal"] and me["personal"] is True and me["is_admin"] is False


def test_session_lets_requests_through_without_the_token(tmp_path):
    p = make_personal(tmp_path)
    c = p.client()
    c.enter()
    assert c.get("/api/me").status == 200
    fresh = p.client()  # another local process without the cookie
    assert fresh.get("/api/me").status == 401


def test_token_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    p = make_personal(tmp_path)
    c = p.client()
    c.enter()
    c.get("/api/me")
    p.client().get("/", query="token=" + "X" * 32)
    assert TOKEN not in caplog.text and "X" * 32 not in caplog.text
    audit = " ".join(str(r) for r in p.rt.wsgi.app.audit.entries())
    assert TOKEN not in audit


def test_cross_origin_post_rejected_same_origin_ok(tmp_path):
    p = make_personal(tmp_path)
    c = p.client()
    c.enter()
    body = {"question": "무엇?"}
    for origin in ("http://evil.example", "https://127.0.0.1:%d" % PORT, "http://127.0.0.1:1", "null",
                   "http://127.0.0.1.evil.example:%d" % PORT):
        r = c.post("/api/query", body, headers={"Origin": origin})
        assert r.status == 403 and r.json() == {"error": "csrf"}, origin
    for origin in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", f"http://[::1]:{PORT}"):
        assert c.post("/api/query", body, headers={"Origin": origin}).status == 200


def test_csrf_header_required(tmp_path):
    p = make_personal(tmp_path)
    c = p.client()
    c.enter()
    r = c.post("/api/query", {"question": "x"}, csrf=False)
    assert r.status == 403


def test_logout_route_still_works_and_ends_session(tmp_path):
    p = make_personal(tmp_path)
    c = p.client()
    c.enter()
    assert c.post("/logout").status == 200
    assert c.get("/api/me").status == 401


def test_login_endpoint_needs_the_launch_token(tmp_path):
    p = make_personal(tmp_path)
    prov = p.rt.wsgi.provider
    assert prov.authenticate({}) is None and prov.authenticate({"launch_token": "x"}) is None
    assert prov.authenticate({"user_id": "me"}) is None and prov.authenticate(None) is None  # type: ignore[arg-type]
    assert prov.authenticate({"launch_token": TOKEN}).id == "me"
    assert TOKEN not in repr(prov)


def test_provider_requires_strong_token():
    with pytest.raises(ValueError):
        PersonalAuthProvider("short")


# ---- bind host ---------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "", "::", "192.168.1.2", "127.0.0.1 ", "127.0.0.2", "example.com"])
def test_bind_host_must_be_exactly_loopback(host):
    with pytest.raises(PersonalConfigError):
        validate_bind_host(host)
    with pytest.raises(PersonalConfigError):
        make_local_server(host, 0, lambda e, s: [])


def test_cli_refuses_non_loopback_host_and_ignores_wiki_host(capsys):
    assert personal_main(["--host", "0.0.0.0", "--no-browser"], {"WIKI_HOST": "0.0.0.0"}) == 2
    assert "127.0.0.1" in capsys.readouterr().err


def test_real_socket_roundtrip_binds_loopback_only_and_checks_host(tmp_path):
    p = make_personal(tmp_path)
    server = make_local_server("127.0.0.1", 0, p.rt.wsgi)
    port = server.server_port
    assert server.server_address[0] == "127.0.0.1"
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        # the runtime was built for PORT, so use its own host list: a request for another port must be refused
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        r = conn.getresponse()
        assert r.status == 403 and b"forbidden" in r.read()
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/?token={TOKEN}", headers={"Host": f"127.0.0.1:{PORT}"})
        r = conn.getresponse()
        assert r.status == 302 and r.getheader("Location") == "/"
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


# ---- the central server must refuse personal mode ------------------------------------------------

def test_central_get_provider_rejects_personal():
    s = CfgSettings("development", "x", "personal", "http://127.0.0.1:1/v1", "m", False)
    with pytest.raises(ValueError):
        get_provider(s)


def test_central_create_app_rejects_personal_provider(tmp_path):
    env = make_web(tmp_path).env
    cfg = WebConfig(session_secret=b"S" * 40, cookie_secure=False)  # personal=False (the default)
    with pytest.raises(RuntimeError, match="personal"):
        create_app(env.settings, None, env.store, env.audit, PersonalAuthProvider(TOKEN), config=cfg)


@pytest.mark.parametrize("var", ["WIKI_MODE", "WIKI_AUTH_PROVIDER"])
def test_central_server_main_refuses_personal(monkeypatch, var):
    monkeypatch.setenv(var, "personal")
    with pytest.raises(SystemExit) as e:
        web_main.main([])
    assert "personal" in str(e.value)


def test_central_app_has_no_personal_endpoints_or_flag(tmp_path):
    w = make_web(tmp_path)
    c = w.client()
    assert c.login("ua").status == 200
    assert c.get("/api/personal/status").status == 404
    assert c.get("/api/personal/info").status == 404
    assert "personal" not in c.get("/api/me").json()
    tok = c.get("/api/login-info").json()["csrf"]
    r = c.request("POST", "/login", json_body={"user_id": "ub"}, headers={"X-CSRF-Token": tok})
    assert "SameSite=Lax" in r.header("Set-Cookie")  # the central server keeps its Lax cookies


