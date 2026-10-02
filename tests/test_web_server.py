"""Server selection (waitress | wsgiref) in llmwiki.web.__main__. waitress is stubbed unless really installed."""
import socket
import sys
import threading
import types
import urllib.request

import pytest

from llmwiki.web import __main__ as entry
from test_web_support import make_web


def _stub_waitress(monkeypatch):
    calls = []
    mod = types.ModuleType("waitress")
    mod.serve = lambda app, **kw: calls.append((app, kw))
    monkeypatch.setitem(sys.modules, "waitress", mod)
    return calls


def _no_waitress(monkeypatch):
    monkeypatch.setitem(sys.modules, "waitress", None)  # import raises ImportError


def test_default_is_wsgiref_without_waitress(monkeypatch):
    _no_waitress(monkeypatch)
    assert entry.resolve_server(None, {}) == "wsgiref"


def test_default_is_waitress_when_importable(monkeypatch):
    _stub_waitress(monkeypatch)
    assert entry.resolve_server(None, {}) == "waitress"
    assert entry.resolve_server(None, {"WIKI_SERVER": "wsgiref"}) == "wsgiref"
    assert entry.resolve_server("wsgiref", {"WIKI_SERVER": "waitress"}) == "wsgiref"  # CLI beats env


def test_explicit_waitress_missing_is_an_error(monkeypatch):
    _no_waitress(monkeypatch)
    with pytest.raises(RuntimeError, match="not installed"):
        entry.resolve_server("waitress", {})
    with pytest.raises(RuntimeError):
        entry.resolve_server(None, {"WIKI_SERVER": "waitress"})


def test_unknown_server_name_rejected(monkeypatch):
    _stub_waitress(monkeypatch)
    with pytest.raises(ValueError):
        entry.resolve_server(None, {"WIKI_SERVER": "gunicorn"})


def test_serve_waitress_passes_config(tmp_path, monkeypatch):
    calls = _stub_waitress(monkeypatch)
    w = make_web(tmp_path, max_threads=17, socket_timeout=12.0, max_upload_bytes=5000)
    entry.serve_waitress(w.app, "127.0.0.1", 9999)
    (app, kw), = calls
    assert app is w.app
    assert kw["host"] == "127.0.0.1" and kw["port"] == 9999 and kw["ident"] == "wiki"
    assert kw["threads"] == 17 and kw["channel_timeout"] == 12
    assert kw["max_request_body_size"] >= 5000  # never smaller than the app's own upload cap


def test_main_uses_waitress_stub_and_wsgiref_flag(tmp_path, monkeypatch):
    calls = _stub_waitress(monkeypatch)
    monkeypatch.setenv("WIKI_ENV", "development")
    monkeypatch.setenv("WIKI_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("WIKI_EMBED_MODEL", "off")
    monkeypatch.delenv("WIKI_SERVER", raising=False)
    entry.main(["--server", "waitress"])
    assert len(calls) == 1 and calls[0][1]["ident"] == "wiki"


def test_main_missing_waitress_exits_nonzero(tmp_path, monkeypatch):
    _no_waitress(monkeypatch)
    monkeypatch.setenv("WIKI_ENV", "development")
    monkeypatch.setenv("WIKI_DATA_DIR", str(tmp_path / "d"))
    with pytest.raises(SystemExit) as ei:
        entry.main(["--server", "waitress"])
    assert "waitress" in str(ei.value)


def test_wsgiref_fallback_still_serves(tmp_path):
    w = make_web(tmp_path)
    srv = entry.build_server(w.env.settings, w.app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/healthz", timeout=5)
        assert r.status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_real_waitress_serves_healthz(tmp_path):
    pytest.importorskip("waitress")
    from waitress.server import create_server

    w = make_web(tmp_path)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = create_server(w.app, host="127.0.0.1", port=port, ident="wiki", threads=4)
    threading.Thread(target=srv.run, daemon=True).start()
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
        assert r.status == 200
        assert r.headers.get("Server") == "wiki"
    finally:
        srv.close()
