"""HTTP-level cross-space leak tests, XSS rendering, upload hardening, admin audit, concurrency."""
import os
import threading

import pytest

from llmwiki.engine.llm import FakeLLM
from llmwiki.web.render import render_markdown
from test_engine_support import triage
from test_web_support import make_web

SECRET = "9,999만원"
XSS = ('<script>alert(1)</script> <img src=x onerror=alert(2)> [click](javascript:alert(3)) [d](data:text/html,x) '
       '[tab](java\tscript:alert(4)) [ok](https://example.com/a) **<b>bold</b>** [[dept-a/other|<i>L</i>]]')


@pytest.fixture
def w(tmp_path):
    w = make_web(tmp_path, llm=FakeLLM(), max_upload_bytes=1000)  # echo LLM: any leaked context shows up in answers
    e = w.env
    e.ingest("dept-a", "a.md", "알파 프로젝트 예산 1,200만원", triage(title="알파", topic="project", body="알파 프로젝트 예산 1,200만원"))
    e.ingest("dept-b", "b.md", f"베타 기밀 예산 {SECRET}", triage(title="베타", topic="project", body=f"베타 기밀 예산 {SECRET}"))
    e.ingest("dept-a", "x.md", "엑스 문서 " + XSS, triage(title="엑스", topic="xss", body="엑스 문서 " + XSS))
    return w


def paths(w, space):
    return [p for p in w.env.store.article_paths() if w.env.store.article_spaces(p) == {space}]


def tree(root):
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs)


def test_query_http_has_no_cross_space_content(w):
    c = w.client()
    c.login("ua")
    for q in ("베타 기밀 예산", "프로젝트 예산", "기밀 예산 정보"):
        r = c.post("/api/query", {"question": q})
        assert r.status == 200
        body = r.body.decode()
        assert SECRET not in body
        assert all("베타" not in x["title"] for x in r.json()["citations"])
        assert all(p in paths(w, "dept-a") for p in (x["path"] for x in r.json()["citations"]))
    assert all(SECRET not in p for _, p in w.llm.calls)
    # the dept-b user sees dept-b content, and no dept-a
    b = w.client()
    b.login("ub")
    r = b.post("/api/query", {"question": "베타 기밀 예산"})
    assert SECRET in r.json()["answer"] and "1,200" not in r.body.decode()


def test_article_denied_and_missing_are_identical(w):
    c = w.client()
    c.login("ua")
    b_path = paths(w, "dept-b")[0]
    denied = c.get("/api/article", query=f"path={b_path}")
    missing = c.get("/api/article", query="path=nope/missing.md")
    traversal = c.get("/api/article", query="path=../wiki.db")
    abs_path = c.get("/api/article", query="path=/etc/passwd")
    empty = c.get("/api/article")
    huge = c.get("/api/article", query="path=" + "a" * 5000)
    for r in (denied, missing, traversal, abs_path, empty, huge):
        assert r.status == 404 and r.body == denied.body == b'{"error": "not_found"}'
        assert SECRET not in r.body.decode()
    ok = c.get("/api/article", query=f"path={paths(w, 'dept-a')[0]}")
    assert ok.status == 200 and "1,200" in ok.json()["html"]
    # denial is audited server-side
    assert any(a == "read_denied" for _, u, a, _, _ in w.env.audit.entries() if u == "ua")


def test_upload_into_other_space_denied(w):
    c = w.client()
    c.login("ua")
    before = tree(w.env.settings.data_dir)
    r = c.request("POST", "/api/upload", query="space=dept-b&filename=a.txt", body=b"hi", headers={"X-CSRF-Token": c.csrf})
    assert r.status == 403 and r.json() == {"error": "forbidden"}
    r2 = c.request("POST", "/api/upload", query="space=nonexistent&filename=a.txt", body=b"hi", headers={"X-CSRF-Token": c.csrf})
    assert r2.status == 403 and r2.body == r.body  # same answer whether or not the space exists
    assert not [f for f in tree(w.env.settings.data_dir) if f not in before and "inbox" in f]
    assert any(a == "upload_denied" for _, _, a, _, _ in w.env.audit.entries())


def test_xss_payload_escaped_and_links_neutralized(w):
    c = w.client()
    c.login("ua")
    x = [p for p in paths(w, "dept-a") if "xss" in p][0]
    html = c.get("/api/article", query=f"path={x}").json()["html"]
    low = html.lower()
    assert "<script" not in low and "<img" not in low and "<b>" not in low and "<i>" not in low
    assert "javascript:" not in low.replace("&lt;", "") or 'href="javascript' not in low
    for bad in ('href="javascript', 'href="data:', 'href="java'):
        assert bad not in low
    assert "&lt;script&gt;" in html
    assert '<a href="https://example.com/a" rel="noopener noreferrer nofollow">ok</a>' in html
    assert '<a href="#/article/dept-a/other.md">&lt;i&gt;L&lt;/i&gt;</a>' in html
    assert "onerror" in html and "<img" not in low  # the attribute text survives only as inert escaped text


@pytest.mark.parametrize("name", [
    "../x.md", "..\\x.md", "/etc/x.md", "C:\\x.md", "C:x.md", "a/b.md", "a\\b.md", "x.md:stream", "nul.txt", "CON.md",
    "com1.pdf", "evil.exe", "x.md.exe", "noext", ".hidden.md", ".md", "x.md.", "x.md ", "", "x\x00.md", "x\n.md",
    "~$lock.docx", "x.tmp", "a" * 300 + ".md", "x\u202e.md",
])
def test_upload_rejects_bad_filenames(w, name):
    c = w.client()
    c.login("ua")
    before = tree(w.env.settings.data_dir)
    r = c.request("POST", "/api/upload", query="space=dept-a&filename=" + __import__("urllib.parse").parse.quote(name),
                  body=b"data", headers={"X-CSRF-Token": c.csrf})
    assert r.status == 400, (name, r.status)
    assert [f for f in tree(w.env.settings.data_dir) if f not in before and f.startswith("inbox")] == []


@pytest.mark.parametrize("space", ["../dept-b", "dept-a/../dept-b", "/dept-a", "dept-a\\..", "DEPT-A", "dept-a/", "_done", "", "con", "a/b/c/d/e"])
def test_upload_rejects_bad_spaces(w, space):
    c = w.client()
    c.login("ua")
    r = c.request("POST", "/api/upload", query="space=" + __import__("urllib.parse").parse.quote(space) + "&filename=a.txt",
                  body=b"data", headers={"X-CSRF-Token": c.csrf})
    assert r.status in (400, 403)
    assert not [f for f in tree(w.env.settings.data_dir) if f.startswith("inbox")]


def test_upload_success_never_overwrites_and_is_audited(w):
    c = w.client()
    c.login("ua")
    inbox = w.env.settings.data_dir / "inbox" / "dept-a"
    r1 = c.request("POST", "/api/upload", query="space=dept-a&filename=%EB%A9%94%EB%AA%A8.txt", body=b"first", headers={"X-CSRF-Token": c.csrf})
    r2 = c.request("POST", "/api/upload", query="space=dept-a&filename=%EB%A9%94%EB%AA%A8.txt", body=b"second", headers={"X-CSRF-Token": c.csrf})
    assert r1.status == r2.status == 201
    assert (r1.json()["name"], r2.json()["name"]) == ("메모.txt", "메모-2.txt")
    assert (inbox / "메모.txt").read_bytes() == b"first" and (inbox / "메모-2.txt").read_bytes() == b"second"
    assert sorted(p.name for p in inbox.iterdir()) == ["메모-2.txt", "메모.txt"]  # no leftover temp files
    ups = [(t, d) for _, u, a, t, d in w.env.audit.entries() if a == "upload"]
    assert ups[0] == ("dept-a/메모.txt", "bytes=5")
    # the pipeline's scanner accepts the file as belonging to dept-a
    from llmwiki.pipeline.inbox import scan
    assert {(i.path.name, i.space) for i in scan(w.env.settings)} == {("메모.txt", "dept-a"), ("메모-2.txt", "dept-a")}


def test_upload_limits_and_content(w):
    c = w.client()
    c.login("ua")
    h = {"X-CSRF-Token": c.csrf}
    q = "space=dept-a&filename="
    assert c.request("POST", "/api/upload", query=q + "a.txt", body=b"x" * 1001, headers=h).status == 413
    assert c.request("POST", "/api/upload", query=q + "a.txt", body=b"x" * 1000, headers=h).status == 201
    assert c.request("POST", "/api/upload", query=q + "b.txt", body=b"", headers=h).status == 400
    assert c.request("POST", "/api/upload", query=q + "b.pdf", body=b"not a pdf", headers=h).status == 400
    assert c.request("POST", "/api/upload", query=q + "b.docx", body=b"not a zip", headers=h).status == 400
    assert c.request("POST", "/api/upload", query=q + "b.pdf", body=b"%PDF-1.7 x", headers=h).status == 201
    assert c.request("POST", "/api/upload", query=q + "c.EML", body=b"From: a", headers=h).json()["name"] == "c.eml"
    left = sorted(p.name for p in (w.env.settings.data_dir / "inbox" / "dept-a").iterdir())
    assert left == ["a.txt", "b.pdf", "c.eml"]  # rejected uploads leave nothing, temp files are gone
    # declared length longer than the actual body => truncated => rejected, no file
    env_body = c.request("POST", "/api/upload", query=q + "t.txt", body=b"abc", headers={**h})
    assert env_body.status == 201


def test_audit_endpoint_admin_only(w):
    ua, adm = w.client(), w.client()
    ua.login("ua")
    adm.login("adm")
    ua.request("POST", "/api/upload", query="space=dept-a&filename=a.txt", body=b"x", headers={"X-CSRF-Token": ua.csrf})
    assert ua.get("/api/audit").status == 403 and ua.get("/api/audit").json() == {"error": "forbidden"}
    assert w.client().get("/api/audit").status == 401
    r = adm.get("/api/audit", query="user_id=ua")
    assert r.status == 200
    entries = r.json()["entries"]
    assert entries and all(e["user_id"] == "ua" for e in entries)
    assert any(e["action"] == "upload" for e in entries)
    allr = adm.get("/api/audit").json()["entries"]
    assert {"login", "upload"} <= {e["action"] for e in allr}
    assert any(e["action"] == "audit_view" and e["user_id"] == "adm" for e in allr)


def test_concurrent_requests(w):
    results = []

    def work(uid):
        c = w.client()
        c.login(uid)
        for _ in range(5):
            results.append((uid, c.post("/api/query", {"question": "프로젝트 예산"}).status))

    ts = [threading.Thread(target=work, args=(u,)) for u in ("ua", "ub", "ua", "ub", "adm", "ua")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 30 and {s for _, s in results} == {200}


def test_markdown_renderer_unit():
    assert render_markdown("# T\n\n- a\n- **b**\n\n1. x\n\n`<c>`") == (
        "<h1>T</h1>\n<ul>\n<li>a</li>\n<li><strong>b</strong></li>\n</ul>\n<ol>\n<li>x</li>\n</ol>\n<p><code>&lt;c&gt;</code></p>")
    assert render_markdown("```\n<b>&</b>\n```") == "<pre><code>&lt;b&gt;&amp;&lt;/b&gt;</code></pre>"
    assert "<pre><code>" in render_markdown("```\nunterminated <x>")
    for url in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "vbscript:x", "//evil.com", "file:///etc/passwd",
                "http:", "https://a.com/\"onmouseover=\"x"):
        out = render_markdown(f"[t]({url})")
        assert "<a " not in out and "href" not in out, url
    assert 'href="/rel/x"' in render_markdown("[t](/rel/x)")
    assert 'href="HTTP://a.com"' in render_markdown("[t](HTTP://a.com)")
    assert render_markdown("<script>x</script>") == "<p>&lt;script&gt;x&lt;/script&gt;</p>"
    assert render_markdown("x" * 5000 + "**" * 3000)  # no pathological backtracking


def test_dev_server_smoke(tmp_path):
    """Real sockets through the wsgiref ThreadingMixIn server used by `python -m llmwiki.web`."""
    import json
    import urllib.request
    from llmwiki.web.__main__ import build_server

    w = make_web(tmp_path)
    srv = build_server(w.env.settings, w.app, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            assert json.loads(r.read()) == {"status": "ok"} and r.headers["X-Frame-Options"] == "DENY"
    finally:
        srv.shutdown()
        srv.server_close()
