"""Review follow-ups: port hijack, single-use token, per-port cookie, home ACL warning, TOCTOU, robustness."""
import os
from pathlib import Path
import socket
import sys

import pytest

from llmwiki.personal.__main__ import make_local_server

ROOT = Path(__file__).resolve().parents[1]
win_only = pytest.mark.skipif(sys.platform != "win32", reason="SO_REUSEADDR hijack semantics are Windows-specific")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@win_only
def test_port_already_bound_with_reuseaddr_is_refused():
    port = _free_port()
    squatter = socket.socket()
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squatter.bind(("127.0.0.1", port))
    squatter.listen()
    try:
        with pytest.raises(OSError):
            make_local_server("127.0.0.1", port, lambda e, s: [])
    finally:
        squatter.close()


@win_only
def test_nobody_can_bind_our_port_afterwards_even_with_reuseaddr():
    port = _free_port()
    server = make_local_server("127.0.0.1", port, lambda e, s: [])
    try:
        thief = socket.socket()
        thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            thief.bind(("127.0.0.1", port))
        thief.close()
    finally:
        server.server_close()


def test_server_class_does_not_allow_address_reuse():
    server = make_local_server("127.0.0.1", 0, lambda e, s: [])
    try:
        assert server.allow_reuse_address is False
    finally:
        server.server_close()


# ---- single-use token --------------------------------------------------------------------------------

from test_personal_support import PORT, TOKEN, make_personal  # noqa: E402


def test_token_is_single_use(tmp_path):
    p = make_personal(tmp_path)
    assert p.client().get("/", query=f"token={TOKEN}").status == 302
    again = p.client()
    r = again.get("/", query=f"token={TOKEN}")
    assert r.status == 401 and r.header("Set-Cookie") is None and not again.cookies


def test_token_only_accepted_on_root_path_and_is_not_consumed_elsewhere(tmp_path):
    p = make_personal(tmp_path)
    c = p.client()
    for path in ("/api/me", "/static/app.js", "/healthz", "/login", "//", "/x"):
        r = c.get(path, query=f"token={TOKEN}")
        assert r.status == 401 and not c.cookies, path
    assert c.get("/", query=f"token={TOKEN}").status == 302  # still valid for the one real exchange


def test_wrong_token_does_not_consume_the_real_one(tmp_path):
    p = make_personal(tmp_path)
    assert p.client().get("/", query="token=" + "x" * 32).status == 401
    assert p.client().get("/", query=f"token={TOKEN}").status == 302


# ---- cookie names per port ---------------------------------------------------------------------------

def test_session_cookie_is_named_per_port_and_instances_do_not_collide(tmp_path):
    a = make_personal(tmp_path / "a", port=PORT)
    b = make_personal(tmp_path / "b", port=PORT + 1)
    ca, cb = a.client(port=PORT), b.client(port=PORT + 1)
    ca.enter()
    cb.enter()
    assert list(ca.cookies) == [f"wiki_sid_{PORT}"] and list(cb.cookies) == [f"wiki_sid_{PORT + 1}"]
    # browsers share cookies across ports of one host: both cookies sent to both instances still resolve correctly
    both = {**ca.cookies, **cb.cookies}
    ca.cookies, cb.cookies = dict(both), dict(both)
    assert ca.get("/api/me").status == 200 and cb.get("/api/me").status == 200
    ca.post("/logout")
    assert cb.get("/api/me").status == 200  # logging out of one instance leaves the other alone


def test_central_cookie_name_is_unchanged(tmp_path):
    from test_web_support import make_web
    w = make_web(tmp_path)
    assert w.app.session_auth.cookie_name == "wiki_sid"


# ---- --check: data folder outside the profile ----------------------------------------------------------

def test_check_warns_when_home_is_outside_the_user_profile(tmp_path, capsys):
    from llmwiki.personal.__main__ import main
    profile = tmp_path / "profile"
    profile.mkdir()
    base = {"LOCALAPPDATA": str(profile / "AppData"), "USERPROFILE": str(profile), "WIKI_LLM_BASE_URL": "http://127.0.0.1:9/v1"}
    main(["--check"], {**base, "WIKI_PERSONAL_HOME": str(tmp_path / "D_MyWiki")})
    out = capsys.readouterr().out
    assert "사용자 프로필 밖" in out and "icacls" in out and "[미검증, 실행하지 않음]" in out
    main(["--check"], {**base, "WIKI_PERSONAL_HOME": str(profile / "AppData" / "LLMWiki")})
    assert "사용자 프로필 밖" not in capsys.readouterr().out


# ---- watch folder: TOCTOU, device names, unstageable paths -----------------------------------------------

from llmwiki.personal import watchfolders  # noqa: E402
from llmwiki.personal.watchfolders import UNSTAGEABLE, WatchIndex, WatchScanner  # noqa: E402


@pytest.fixture
def wenv(tmp_path):
    home, watch = tmp_path / "home", tmp_path / "Docs"
    (home / "inbox").mkdir(parents=True)
    watch.mkdir()
    index = WatchIndex(home / "watch_index.db")
    yield home, watch, WatchScanner(home, [watch], index, min_age=0.0), index
    index.close()


def _staged(home):
    return [p for p in (home / "inbox" / "watch").rglob("*") if p.is_file()]


def test_file_swapped_between_lstat_and_open_is_not_staged(wenv, monkeypatch):
    home, watch, scanner, _ = wenv
    doc = watch / "a.md"
    doc.write_text("original content")
    other = watch.parent / "other.md"
    other.write_text("a completely different and longer file body")
    real_open = open

    def swapping_open(path, *a, **kw):
        if str(path) == str(doc):
            os.replace(other, doc)  # the swap happens right before the handle is opened
        return real_open(path, *a, **kw)

    monkeypatch.setattr(watchfolders, "open", swapping_open, raising=False)
    assert scanner.scan()["staged"] == 0
    monkeypatch.undo()
    assert not _staged(home) and not list((home / "inbox" / "watch").glob(".copy-*"))


def test_file_replaced_by_a_link_before_the_copy_is_not_followed(wenv, monkeypatch):
    home, watch, scanner, _ = wenv
    doc = watch / "a.md"
    doc.write_text("original")
    secret = watch.parent / "secret.md"
    secret.write_text("outside")
    real_lstat = os.lstat
    def fake_lstat(path, *a, **kw):
        st = real_lstat(path, *a, **kw)
        if str(path) == str(doc):
            fields = list(st)
            fields[0] = (fields[0] & ~0o170000) | 0o120000  # looks like a symlink
            return os.stat_result(fields)
        return st
    monkeypatch.setattr(watchfolders.os, "lstat", fake_lstat)
    assert scanner.scan()["staged"] == 0
    monkeypatch.undo()
    assert not _staged(home)


def test_reparse_bit_set_after_open_aborts(wenv, monkeypatch):
    home, watch, scanner, _ = wenv
    doc = watch / "a.md"
    doc.write_text("original")
    real_lstat, calls = os.lstat, {"n": 0}

    def fake_lstat(path, *a, **kw):
        st = real_lstat(path, *a, **kw)
        if str(path) == str(doc):
            calls["n"] += 1
            if calls["n"] >= 2:  # the post-copy re-check
                class S:  # same numbers plus a reparse point attribute
                    st_size, st_mtime_ns, st_ino, st_dev, st_mode = st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev, st.st_mode
                    st_file_attributes = 0x400
                return S
        return st

    monkeypatch.setattr(watchfolders.os, "lstat", fake_lstat)
    assert scanner.scan()["staged"] == 0
    monkeypatch.undo()
    assert not _staged(home)


@pytest.mark.parametrize("name", ["nul.md", "NUL.txt", "con.md", "com1.md", "LPT9.pdf", "aux.docx", "prn.md",
                                  "nul .md", "nul..md", "con.backup.md", "NUL. .md"])
def test_device_names_are_not_staged_and_not_retried(wenv, name):
    home, watch, scanner, index = wenv
    (watch / name).write_text("x")
    assert scanner.scan()["staged"] == 0 and not _staged(home)
    row = index.get(str(next(watch.iterdir())))
    assert row is not None and row[2] == UNSTAGEABLE
    assert scanner.scan()["unchanged"] == 1  # not looked at again until size/mtime change


def test_safe_name_rejects_devices_and_keeps_normal_names():
    assert watchfolders._safe_name("회의록 2026.md") == "회의록 2026.md"
    assert watchfolders._safe_name("a:b.md") == "a_b.md"
    with pytest.raises(ValueError):
        watchfolders._safe_name("nul")


def test_overlong_staging_path_is_recorded_and_not_recopied_until_it_changes(wenv):
    home, watch, scanner, index = wenv
    scanner.stage = home / "inbox" / "watch"
    long_name = "a" * 60 + ".md"
    doc = watch / long_name
    doc.write_text("x")
    scanner.stage = home / ("s" * 200) / "inbox"  # pushes the staged path beyond the limit
    assert scanner.scan()["staged"] == 0
    assert index.get(str(doc))[2] == UNSTAGEABLE
    scanner.stage = home / "inbox" / "watch"
    assert scanner.scan() == {"staged": 0, "unchanged": 1, "waiting": 0, "skipped": 0}
    doc.write_text("changed content")
    assert scanner.scan()["staged"] == 1  # a size/mtime change makes it eligible again


# ---- retry policy for a bare 400, bounded wait for questions ---------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

from llmwiki.engine.llm import FakeLLM, LLMError  # noqa: E402
from llmwiki.personal.runtime import question_wait  # noqa: E402
from llmwiki.personal.serialize import BACKGROUND, INTERACTIVE, GateTimeout, PriorityGate, SerializedLLM  # noqa: E402
from llmwiki.personal.settings import PersonalConfigError  # noqa: E402
from test_personal_serialize import Scripted, llm_error  # noqa: E402


def _proxy(inner, sleeps, **kw):
    return SerializedLLM(inner, PriorityGate(), INTERACTIVE, sleep=sleeps.append, **kw)


def test_bare_400_is_retried_exactly_once():
    sleeps, inner = [], Scripted(llm_error(400), llm_error(400), "never")
    with pytest.raises(LLMError):
        _proxy(inner, sleeps).complete("s", "p")
    assert inner.calls == 2 and sleeps == [2.0]
    sleeps, inner = [], Scripted(llm_error(400), "ok")
    assert _proxy(inner, sleeps).complete("s", "p") == "ok" and sleeps == [2.0]


def test_a_400_after_an_earlier_retry_is_not_retried_again():
    sleeps, inner = [], Scripted(llm_error(503), llm_error(400), "never")
    with pytest.raises(LLMError):
        _proxy(inner, sleeps).complete("s", "p")
    assert inner.calls == 2


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_5xx_and_429_keep_three_tries(status):
    sleeps, inner = [], Scripted(*[llm_error(status)] * 3, "never")
    with pytest.raises(LLMError):
        _proxy(inner, sleeps).complete("s", "p")
    assert inner.calls == 3 and sleeps == [2.0, 5.0]


def test_question_gives_up_with_a_busy_error_after_the_bounded_wait():
    gate, release = PriorityGate(), threading.Event()
    bg_started = threading.Event()

    def slow(system, prompt):
        bg_started.set()
        release.wait(5)
        return "bg"

    bg = SerializedLLM(FakeLLM(slow), gate, BACKGROUND)
    t = threading.Thread(target=bg.complete, args=("s", "p"))
    t.start()
    assert bg_started.wait(5)
    fg = SerializedLLM(FakeLLM(lambda s, p: "fg"), gate, INTERACTIVE, wait=0.2)
    t0 = time.monotonic()
    with pytest.raises(GateTimeout) as e:
        fg.complete("s", "q")
    assert time.monotonic() - t0 < 3 and isinstance(e.value, LLMError) and e.value.busy
    assert not gate._heap  # the abandoned ticket is gone, later callers are not blocked behind it
    release.set()
    t.join(5)
    assert fg.complete("s", "q") == "fg"


def test_gate_timeout_is_not_retried():
    gate = PriorityGate()
    gate.acquire(BACKGROUND)
    sleeps = []
    fg = SerializedLLM(FakeLLM(lambda s, p: "x"), gate, INTERACTIVE, wait=0.05, sleep=sleeps.append)
    with pytest.raises(GateTimeout):
        fg.complete("s", "q")
    assert sleeps == []
    gate.release()


def test_web_shows_friendly_busy_status_when_the_gate_times_out(tmp_path):
    p = make_personal(tmp_path, {"WIKI_PERSONAL_QUESTION_WAIT": "0.1"})
    p.drop("a.md", "# 주간 회의\n\n내용")
    p.settle()
    c = p.client()
    c.enter()
    gate = p.rt.gate
    gate.acquire(BACKGROUND)  # a document is "being processed"
    try:
        r = c.post("/api/query", {"question": "주간 회의 내용"})
    finally:
        gate.release()
    assert r.status == 503 and r.json() == {"error": "llm_busy"}
    assert c.post("/api/query", {"question": "주간 회의 내용"}).status == 200
    from llmwiki.web.ui_js import APP_JS
    assert "llm_busy" in APP_JS and "문서 처리 중이라 답변이 지연됩니다. 잠시 후 다시 시도하세요" in APP_JS


def test_question_wait_setting():
    assert question_wait({}) == 300.0 and question_wait({"WIKI_PERSONAL_QUESTION_WAIT": "12.5"}) == 12.5
    for bad in ("x", "0", "-3"):
        with pytest.raises(PersonalConfigError):
            question_wait({"WIKI_PERSONAL_QUESTION_WAIT": bad})


def test_docs_mention_gitignored_env_file_and_single_use_token():
    text = (ROOT / "docs" / "PERSONAL-MODE.md").read_text(encoding="utf-8")
    assert ".env.personal" in text and ".gitignore" in text and "한 번만" in text
    assert ".env.personal" in (ROOT / ".gitignore").read_text(encoding="utf-8")
