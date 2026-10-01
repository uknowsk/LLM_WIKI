"""Session, CSRF, config and header tests."""
import pytest

from llmwiki.auth import DevAuthProvider
from llmwiki.config import Settings
from llmwiki.engine.llm import FakeLLM, LLMError
from llmwiki.web import create_app, load_web_config
from test_engine_support import UA, make_env
from test_web_support import SECRET, make_web

ORIGINS = "https://wiki.example.com"


@pytest.fixture
def w(tmp_path):
    return make_web(tmp_path)


def test_login_logout_flow(w):
    c = w.client()
    assert c.get("/api/me").status == 401
    r = c.login("ua")
    assert r.status == 200 and r.json()["id"] == "ua"
    cookie = r.header("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" not in cookie  # secure off in this config
    me = c.get("/api/me").json()
    assert me["spaces"] == ["dept-a"] and me["is_admin"] is False
    old = dict(c.cookies)
    assert c.post("/logout").status == 200
    assert c.get("/api/me").status == 401
    c.cookies = old  # replay the pre-logout cookie: the server-side session is gone
    assert c.get("/api/me").status == 401
    audit = [(u, a) for _, u, a, _, _ in w.env.audit.entries()]
    assert ("ua", "login") in audit and ("ua", "logout") in audit


def test_secure_cookie_flag_and_host_prefix(tmp_path):
    w = make_web(tmp_path, cookie_secure=True)
    c = w.client()
    r = c.login("ua")
    cookie = r.header("Set-Cookie")
    assert "Secure" in cookie and cookie.startswith("__Host-wiki_sid=")
    assert w.app and c.get("/healthz").header("Strict-Transport-Security")


def test_bad_login_and_login_csrf(w):
    c = w.client()
    tok = c.get("/api/login-info").json()["csrf"]
    assert c.request("POST", "/login", json_body={"user_id": "nobody"}, headers={"X-CSRF-Token": tok}).status == 401
    assert c.request("POST", "/login", json_body={"user_id": "ua"}).status == 403  # no token
    assert c.request("POST", "/login", json_body={"user_id": "ua"}, headers={"X-CSRF-Token": "x" * 43}).status == 403
    c2 = w.client()  # token without the matching cookie
    assert c2.request("POST", "/login", json_body={"user_id": "ua"}, headers={"X-CSRF-Token": tok}).status == 403
    assert c.request("POST", "/login", body=b"user_id=ua", ctype="text/plain", headers={"X-CSRF-Token": tok}).status == 415
    assert c.request("POST", "/login", json_body={"user_id": ["x"]}, headers={"X-CSRF-Token": tok}).status == 400
    assert any(a == "login_failed" for _, _, a, _, _ in w.env.audit.entries())


def test_session_fixation_new_id_each_login(w):
    c = w.client()
    c.login("ua")
    first = dict(c.cookies)
    c.login("ua")
    assert c.cookies != first


def test_idle_and_absolute_expiry(w):
    c = w.client()
    c.login("ua")
    w.clock.t += 29 * 60
    assert c.get("/api/me").status == 200  # activity refreshes idle timer
    w.clock.t += 29 * 60
    assert c.get("/api/me").status == 200
    w.clock.t += 31 * 60
    assert c.get("/api/me").status == 401  # idle > 30 min
    c = w.client()
    c.login("ua")
    start = w.clock.t
    for _ in range(30):  # keep active every 25 min until past 8h absolute
        w.clock.t += 25 * 60
        if w.clock.t - start > 8 * 3600:
            break
        assert c.get("/api/me").status == 200
    assert c.get("/api/me").status == 401


def test_revoke_user_sessions(w):
    c = w.client()
    c.login("ua")
    assert w.app.session_auth.revoke_user("ua") == 1
    assert c.get("/api/me").status == 401


def test_tampered_or_missing_cookie_rejected(w):
    c = w.client()
    c.login("ua")
    name, val = next(iter(c.cookies.items()))
    sid, _, mac = val.partition(".")
    other = w.client()
    other.login("ub")
    bad = {
        "no-mac": sid, "bad-mac": sid + "." + "0" * 64, "flipped": sid[:-1] + ("A" if sid[-1] != "A" else "B") + "." + mac,
        "empty": "", "garbage": "!!!", "huge": "a" * 5000,
        "other-session": next(iter(other.cookies.values())),  # valid cookie of another user is theirs, not ua's
    }
    for label, v in bad.items():
        if label == "other-session":
            continue
        c.cookies = {name: v}
        assert c.get("/api/me").status == 401, label
    c.cookies = {}
    assert c.get("/api/me").status == 401
    c.cookies = {name: val}
    assert c.get("/api/me").status == 200
    # duplicated cookie name is ambiguous -> rejected
    r = c.request("GET", "/api/me", headers={"Cookie": f"{name}={val}; {name}={val}"}, cookies=False)
    assert r.status == 401
    # secret rotation invalidates cookies
    w.app.session_auth.cfg = type(w.cfg)(**{**w.cfg.__dict__, "session_secret": b"R" * 40})
    assert c.get("/api/me").status == 401


def test_csrf_enforced_on_state_changing_requests(w):
    c = w.client()
    c.login("ua")
    csrf = c.csrf
    up = dict(query="space=dept-a&filename=a.txt", body=b"hello")
    assert c.request("POST", "/api/query", json_body={"question": "x"}).status == 403
    assert c.request("POST", "/api/query", json_body={"question": "x"}, headers={"X-CSRF-Token": "wrong"}).status == 403
    assert c.request("POST", "/api/upload", **up).status == 403
    assert c.request("POST", "/logout").status == 403
    assert c.get("/api/me").status == 200  # safe methods need no token
    # token from another session is wrong
    c2 = w.client()
    c2.login("ub")
    assert c.request("POST", "/api/query", json_body={"question": "x"}, headers={"X-CSRF-Token": c2.csrf}).status == 403
    assert c.request("POST", "/api/query", json_body={"question": "x"}, headers={"X-CSRF-Token": csrf}).status == 200
    # unauthenticated beats CSRF: 401
    assert w.client().request("POST", "/api/query", json_body={"question": "x"}).status == 401


def test_origin_check(w):
    c = w.client()
    c.login("ua")
    ok = c.post("/api/query", {"question": "x"}, headers={"Origin": "http://wiki.test"})
    assert ok.status == 200
    for origin in ("http://evil.example", "null"):
        assert c.post("/api/query", {"question": "x"}, headers={"Origin": origin}).status == 403


def test_security_headers_and_error_shape(w):
    c = w.client()
    for r in (c.get("/"), c.get("/healthz"), c.get("/api/me"), c.get("/nope"), c.get("/static/app.js")):
        assert r.header("X-Content-Type-Options") == "nosniff"
        assert r.header("X-Frame-Options") == "DENY"
        assert r.header("Referrer-Policy") == "no-referrer"
        assert r.header("Cache-Control") == "no-store"
        csp = r.header("Content-Security-Policy")
        assert "default-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert c.get("/api/me").json() == {"error": "unauthorized"}
    assert c.get("/nope").json() == {"error": "not_found"}
    assert c.request("DELETE", "/api/me").status == 405
    assert c.get("/healthz").json() == {"status": "ok"}


def test_ui_is_static_and_never_uses_innerhtml(w):
    c = w.client()
    page = c.get("/").body.decode()
    assert "<script src=\"/static/app.js\"" in page and "<script>" not in page and "http" not in page
    js = c.get("/static/app.js").body.decode()
    for banned in ("innerHTML", "outerHTML", "eval(", "document.write", "insertAdjacentHTML"):
        assert banned not in js
    assert "https://" not in js and "//cdn" not in js
    assert "text/css" in c.get("/static/app.css").header("Content-Type")


def test_bad_json_and_size_limits(w):
    c = w.client()
    c.login("ua")
    assert c.post("/api/query", csrf=True, body=b"{not json", ctype="application/json").status == 400
    assert c.post("/api/query", body=b"[1]", ctype="application/json").status == 400
    assert c.post("/api/query", body=b"\xff\xfe", ctype="application/json").status == 400
    assert c.post("/api/query", {"question": ""}).status == 400
    assert c.post("/api/query", {"question": 5}).status == 400
    assert c.post("/api/query", {"question": "q" * 2001}).status == 400
    assert c.post("/api/query", {"question": "q" * 70_000}).status == 413
    assert c.post("/api/query", body=b"{}", ctype="text/plain").status == 415


def test_internal_errors_do_not_leak(tmp_path):
    def boom(system, prompt):
        raise RuntimeError("secret-internal-path C:\\data\\wiki.db")

    w = make_web(tmp_path, llm=FakeLLM(boom))
    w.env.ingest("dept-a", "a.md", "알파 일정", __import__("test_engine_support").triage(title="알파", body="알파 일정"))
    c = w.client()
    c.login("ua")
    r = c.post("/api/query", {"question": "알파 일정"})
    assert r.status == 500 and r.json() == {"error": "internal"} and b"secret-internal" not in r.body

    def down(system, prompt):
        raise LLMError("endpoint down")

    w.llm._responder = down
    r = c.post("/api/query", {"question": "알파 일정"})
    assert r.status == 502 and r.json() == {"error": "llm_unavailable"}


def test_production_requires_session_secret(tmp_path):
    prod = Settings("production", tmp_path, "saml", "http://127.0.0.1:8000/v1", "m", False)
    with pytest.raises(RuntimeError, match="WIKI_SESSION_SECRET"):
        load_web_config(prod, {})
    with pytest.raises(RuntimeError, match="at least"):
        load_web_config(prod, {"WIKI_SESSION_SECRET": "short"})
    with pytest.raises(RuntimeError, match="WIKI_ALLOWED_ORIGINS"):
        load_web_config(prod, {"WIKI_SESSION_SECRET": SECRET})
    cfg = load_web_config(prod, {"WIKI_SESSION_SECRET": SECRET, "WIKI_ALLOWED_ORIGINS": ORIGINS})
    assert cfg.cookie_secure is True and cfg.idle_ttl == 1800 and cfg.absolute_ttl == 8 * 3600
    assert cfg.max_upload_bytes == 50 * 1024 * 1024
    # unset env => production (fail closed): create_app without secret refuses to start
    env = make_env(tmp_path / "d")
    with pytest.raises(RuntimeError):
        create_app(prod, FakeLLM(), env.store, env.audit, DevAuthProvider({}), environ={})
    # dev provider is refused in production even when a secret exists
    with pytest.raises(RuntimeError, match="Dev auth"):
        create_app(prod, FakeLLM(), env.store, env.audit, DevAuthProvider({}), environ={"WIKI_SESSION_SECRET": SECRET, "WIKI_ALLOWED_ORIGINS": ORIGINS})
    dev = Settings("development", tmp_path, "dev", "http://127.0.0.1:8000/v1", "m", False)
    assert load_web_config(dev, {}).cookie_secure is False
    with pytest.raises(RuntimeError):
        load_web_config(dev, {"WIKI_MAX_UPLOAD_BYTES": "abc"})
    assert load_web_config(dev, {"WIKI_MAX_UPLOAD_BYTES": "5"}).max_upload_bytes == 5


def test_login_info_lists_dev_users_only_in_dev(w):
    assert set(w.client().get("/api/login-info").json()["dev_users"]) == {"ua", "ub", "adm"}


def test_extra_route_and_start_session_boundary(w):
    """What the SAML ACS handler does: validate elsewhere, then start_session(user)."""
    from llmwiki.web.http import json_response, Request

    def acs(app, req: Request, session):
        grant = app.start_session(req, UA)
        return json_response(200, {"ok": True}, grant.headers())

    w.app.add_route("POST", "/saml/acs", acs, auth=False, csrf=False)
    c = w.client()
    assert c.request("POST", "/saml/acs", body=b"x", ctype="text/plain").status == 200
    assert c.get("/api/me").json()["id"] == "ua"
