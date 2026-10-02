"""Gap fixes: trusted-proxy client address, Request.read_form, audit prune/export CLI (synthetic data only)."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from llmwiki.audit import AuditLog
from llmwiki.audit_admin import main as admin_main
from llmwiki.web.app import client_addr
from llmwiki.web.http import HttpError, Request
from llmwiki.web.webconfig import load_web_config
from test_web_support import make_web

TRUST = frozenset({"10.0.0.1", "10.0.0.2"})


def env(peer, xff=None):
    e = {"REMOTE_ADDR": peer}
    if xff is not None:
        e["HTTP_X_FORWARDED_FOR"] = xff
    return e


def test_default_ignores_xff():
    assert client_addr(env("10.0.0.1", "1.2.3.4"), frozenset()) == "10.0.0.1"


def test_trusted_peer_uses_rightmost_untrusted():
    assert client_addr(env("10.0.0.1", "1.2.3.4"), TRUST) == "1.2.3.4"
    assert client_addr(env("10.0.0.1", "9.9.9.9, 1.2.3.4, 10.0.0.2"), TRUST) == "1.2.3.4"  # left side is attacker-controlled
    assert client_addr(env("10.0.0.1", "10.0.0.2"), TRUST) == "10.0.0.1"  # nothing but proxies
    assert client_addr(env("10.0.0.1"), TRUST) == "10.0.0.1"


def test_untrusted_peer_cannot_spoof():
    assert client_addr(env("6.6.6.6", "1.2.3.4"), TRUST) == "6.6.6.6"
    assert client_addr(env("6.6.6.6", "10.0.0.1"), TRUST) == "6.6.6.6"


def test_garbage_xff_falls_back_to_peer():
    assert client_addr(env("10.0.0.1", "1.2.3.4, not-an-ip"), TRUST) == "10.0.0.1"
    assert client_addr(env("10.0.0.1", ""), TRUST) == "10.0.0.1"


def test_config_parses_and_rejects_proxies():
    from llmwiki.config import Settings
    s = Settings("development", None, "dev", "http://127.0.0.1:1/v1", "m", False)
    assert load_web_config(s, {"WIKI_TRUSTED_PROXIES": " 10.0.0.1 , ::1 "}).trusted_proxies == {"10.0.0.1", "::1"}
    assert load_web_config(s, {}).trusted_proxies == frozenset()
    with pytest.raises(RuntimeError):
        load_web_config(s, {"WIKI_TRUSTED_PROXIES": "10.0.0.0/8"})


def _login_info(app, peer, xff=None):
    e = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/login-info", "QUERY_STRING": "", "HTTP_HOST": "wiki.test",
         "wsgi.input": io.BytesIO(b""), "REMOTE_ADDR": peer}
    if xff:
        e["HTTP_X_FORWARDED_FOR"] = xff
    out = {}
    app(e, lambda st, h, exc=None: out.update(s=int(st.split()[0])))
    return out["s"]


def test_rate_limit_per_client_behind_proxy(tmp_path):
    w = make_web(tmp_path, rate_login_per_min=2, trusted_proxies=frozenset({"10.0.0.1"}))
    assert [_login_info(w.app, "10.0.0.1", "1.1.1.1") for _ in range(3)] == [200, 200, 429]
    assert _login_info(w.app, "10.0.0.1", "2.2.2.2") == 200  # another user behind the same proxy
    # an attacker rotating a forged header from an untrusted peer stays in one bucket
    assert [_login_info(w.app, "6.6.6.6", f"7.7.7.{i}") for i in range(3)] == [200, 200, 429]


def test_default_shares_bucket_behind_proxy(tmp_path):
    w = make_web(tmp_path, rate_login_per_min=2)
    assert [_login_info(w.app, "10.0.0.1", f"1.1.1.{i}") for i in range(3)] == [200, 200, 429]


# ---- read_form ----------------------------------------------------------------------------------
FORM = "application/x-www-form-urlencoded"


def form_req(body: bytes, ctype=FORM, length=None):
    e = {"REQUEST_METHOD": "POST", "PATH_INFO": "/saml/acs", "wsgi.input": io.BytesIO(body),
         "CONTENT_LENGTH": str(len(body) if length is None else length)}
    if ctype:
        e["CONTENT_TYPE"] = ctype
    return Request(e)


def test_read_form_ok():
    r = form_req(b"SAMLResponse=abc%3D%3D&RelayState=r+1&RelayState=x&empty=")
    assert r.read_form(1000) == {"SAMLResponse": ["abc=="], "RelayState": ["r 1", "x"], "empty": [""]}
    assert form_req(b"").read_form(10) == {}
    assert form_req(b"a=%ED%95%9C", FORM + "; charset=UTF-8").read_form(100) == {"a": ["한"]}


MANY = "&".join(f"k{i}=1" for i in range(60)).encode()


@pytest.mark.parametrize("kw,status", [
    (dict(body=b"a=1", ctype="application/json"), 415),
    (dict(body=b"a=1", ctype=None), 415),
    (dict(body=b"a=1", ctype="multipart/form-data"), 415),
    (dict(body=b"a=" + b"x" * 100), 413),
    (dict(body=b"a=1", length=999999999999999), 413),
    (dict(body=b"a=1", length="abc"), 400),
    (dict(body=b"a=1", length=10), 400),  # short body
    (dict(body="a=한".encode()), 400),  # non-ASCII
    (dict(body=b"a=%FF"), 400),  # invalid UTF-8 escape
    (dict(body=b"novalue"), 400),
    (dict(body=MANY), 400),  # too many fields
])
def test_read_form_rejects(kw, status):
    with pytest.raises(HttpError) as ei:
        form_req(**kw).read_form(max_bytes=5000 if kw["body"] is MANY else 50)
    assert ei.value.status == status


def test_read_form_missing_length():
    e = {"REQUEST_METHOD": "POST", "PATH_INFO": "/", "wsgi.input": io.BytesIO(b"a=1"), "CONTENT_TYPE": FORM}
    with pytest.raises(HttpError) as ei:
        Request(e).read_form(100)
    assert ei.value.status == 411


# ---- audit prune / export --------------------------------------------------------------------------
def seeded(tmp_path):
    a = AuditLog(tmp_path / "a.db")
    now = datetime.now(timezone.utc)
    for days in (200, 100, 10, 0):
        ts = (now - timedelta(days=days)).isoformat(timespec="seconds")
        a._db.execute("INSERT INTO audit (ts, user_id, action, target, detail) VALUES (?,?,?,?,?)",
                      (ts, "u1", "query", f"d{days}", ""))
    a._db.commit()
    return a


def test_prune_deletes_old_and_records_one_row(tmp_path):
    a = seeded(tmp_path)
    assert a.prune(30) == 2
    rows = a.entries()
    assert [r[3] for r in rows if r[2] == "query"] == ["d10", "d0"]
    prune_rows = [r for r in rows if r[2] == "audit_prune"]
    assert len(prune_rows) == 1 and "deleted=2" in prune_rows[0][4] and "cutoff=" in prune_rows[0][4]


@pytest.mark.parametrize("days", [29, 0, -5, True, "40", 40.0])
def test_prune_refuses_short_or_bad_days(tmp_path, days):
    a = seeded(tmp_path)
    with pytest.raises(ValueError):
        a.prune(days)
    assert len(a.entries()) == 4


def test_web_layer_cannot_prune(tmp_path):
    w = make_web(tmp_path)
    with pytest.raises(AttributeError):
        w.app.audit.prune(30)
    assert not any("prune" in p for _, p in w.app._routes)


def test_cli_prune_requires_yes_and_min_days(tmp_path, capsys):
    a = seeded(tmp_path)
    a.close()
    db = str(tmp_path / "a.db")
    assert admin_main(["--db", db, "prune", "--days", "30"]) == 2  # dry run
    assert admin_main(["--db", db, "prune", "--days", "5", "--yes"]) == 2
    b = AuditLog(db)
    assert len(b.entries()) == 4
    b.close()
    assert admin_main(["--db", db, "prune", "--days", "30", "--yes"]) == 0
    assert "deleted 2" in capsys.readouterr().out


def test_cli_export(tmp_path):
    a = seeded(tmp_path)
    a.close()
    db, out = str(tmp_path / "a.db"), tmp_path / "e.jsonl"
    since = (datetime.now(timezone.utc) - timedelta(days=50)).isoformat()
    assert admin_main(["--db", db, "export", "--since", since, "--out", str(out)]) == 0
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert [x["target"] for x in lines] == ["d10", "d0"]
    assert admin_main(["--db", db, "export", "--since", since, "--out", str(out)]) == 2  # never overwrites
    assert admin_main(["--db", db, "export", "--since", "garbage", "--out", str(tmp_path / "n.jsonl")]) == 2
