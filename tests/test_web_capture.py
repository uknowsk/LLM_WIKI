"""Quick capture (POST /api/capture): a logged-in user drops a note/clipping into inbox/<their space>/.

Same trust rules as upload: the space must be one of the user's spaces, the client never chooses a path,
nothing is fetched from the URL, every capture is audited, and a denied capture leaves nothing behind.
"""
import pytest

from llmwiki.engine.llm import FakeLLM
from test_web_support import make_web


@pytest.fixture
def w(tmp_path):
    return make_web(tmp_path, llm=FakeLLM(), max_upload_bytes=5000)


def _login(w, uid="ua"):
    c = w.client()
    assert c.login(uid).status == 200
    return c


def _inbox_files(w):
    root = w.env.settings.data_dir / "inbox"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_capture_into_other_space_denied_and_leaves_nothing(w):
    c = _login(w)
    before = _inbox_files(w)
    r = c.post("/api/capture", {"space": "dept-b", "text": "비밀 메모"})
    r2 = c.post("/api/capture", {"space": "nonexistent", "text": "비밀 메모"})
    assert r.status == 403 and r.json() == {"error": "forbidden"}
    assert r2.status == 403 and r2.body == r.body  # same answer whether or not the space exists
    assert _inbox_files(w) == before
    assert any(a == "capture_denied" for _, _, a, _, _ in w.env.audit.entries())


def test_capture_requires_session_and_csrf(w):
    anon = w.client()
    assert anon.post("/api/capture", {"space": "dept-a", "text": "x"}, csrf=False).status in (401, 403)
    c = _login(w)
    assert c.post("/api/capture", {"space": "dept-a", "text": "x"}, csrf=False).status == 403
    assert _inbox_files(w) == []


def test_capture_writes_note_with_title_url_and_text(w):
    c = _login(w)
    r = c.post("/api/capture", {"space": "dept-a", "title": "회의 메모", "url": "https://example.com/a?b=1",
                                "text": "예산은 1,200만원\n둘째 줄"})
    assert r.status == 201
    body = r.json()
    assert body["ok"] and body["space"] == "dept-a" and body["name"].endswith("회의 메모.md")
    files = _inbox_files(w)
    assert [f.name for f in files] == [body["name"]] and files[0].parent.name == "dept-a"
    text = files[0].read_text(encoding="utf-8")
    assert "# 회의 메모" in text and "https://example.com/a?b=1" in text and "예산은 1,200만원\n둘째 줄" in text
    assert any(a == "capture" for _, u, a, _, _ in w.env.audit.entries() if u == "ua")


def test_title_cannot_escape_the_space_folder(w):
    c = _login(w)
    for title in ("../../evil", "..\\..\\evil", "a/b:c*d?", "CON", "‮gnp.exe", "   ", ""):
        r = c.post("/api/capture", {"space": "dept-a", "title": title, "text": "내용"})
        assert r.status == 201, title
    root = (w.env.settings.data_dir / "inbox").resolve()
    for f in _inbox_files(w):
        assert f.resolve().parent == root / "dept-a" and f.suffix == ".md"
    assert len(_inbox_files(w)) == 7  # nothing overwritten, nothing outside the space


def test_same_title_twice_keeps_both(w):
    c = _login(w)
    for _ in range(2):
        assert c.post("/api/capture", {"space": "dept-a", "title": "메모", "text": "내용"}).status == 201
    assert len(_inbox_files(w)) == 2


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "ftp://x/y", "http://a b/c", "http://x/\ny", "https://" + "a" * 2100])
def test_bad_url_rejected(w, url):
    c = _login(w)
    r = c.post("/api/capture", {"space": "dept-a", "url": url, "text": "내용"})
    assert r.status == 400 and r.json() == {"error": "bad_url"}
    assert _inbox_files(w) == []


@pytest.mark.parametrize("payload,status,err", [
    ({"space": "dept-a", "text": ""}, 400, "empty_text"),
    ({"space": "dept-a", "text": "   \n "}, 400, "empty_text"),
    ({"space": "dept-a", "text": 5}, 400, "bad_request"),
    ({"space": "dept-a", "text": "x", "title": 5}, 400, "bad_request"),
    ({"space": "dept-a", "text": "x", "url": 5}, 400, "bad_request"),
    ({"space": 7, "text": "x"}, 400, "bad_space"),
    ({"space": "../x", "text": "x"}, 400, "bad_space"),
    ({"text": "x"}, 400, "bad_space"),
    ({"space": "dept-a", "text": "가" * 2000}, 413, "too_large"),
])
def test_invalid_input_rejected(w, payload, status, err):
    c = _login(w)
    r = c.post("/api/capture", payload)
    assert r.status == status and r.json() == {"error": err}
    assert _inbox_files(w) == []


def test_nul_and_control_characters_are_stripped_not_stored(w):
    c = _login(w)
    assert c.post("/api/capture", {"space": "dept-a", "text": "앞\x00뒤\x07끝\n줄"}).status == 201
    text = _inbox_files(w)[0].read_text(encoding="utf-8")
    assert "\x00" not in text and "\x07" not in text and "앞뒤끝\n줄" in text


def test_audit_has_size_not_content(w):
    c = _login(w)
    c.post("/api/capture", {"space": "dept-a", "title": "t", "text": "아주 비밀스러운 본문 9,999만원"})
    dump = " ".join(str(e) for e in w.env.audit.entries())
    assert "9,999" not in dump and "비밀스러운" not in dump
