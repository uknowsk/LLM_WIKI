"""End to end in one process: inbox + watch folders -> pipeline -> wiki -> question, all as space 'personal'."""
import json

from llmwiki.personal.worker import scan_inbox
from test_personal_support import make_personal

DOC = "# 주간 회의\n\n신규 서버 증설은 11월로 결정했다.\n"


def articles(p):
    return p.rt.wsgi.app.store.article_paths()


def test_flat_inbox_file_becomes_a_personal_article_and_moves_to_done(tmp_path):
    p = make_personal(tmp_path)
    f = p.drop("회의.md", DOC)
    p.settle()
    assert not f.exists()
    assert (p.home / "inbox" / "_done" / "personal" / "회의.md").exists()
    store = p.rt.wsgi.app.store
    [path] = articles(p)
    assert store.article_spaces(path) == frozenset({"personal"})
    raws = list((p.home / "raw" / "personal").glob("*.md"))
    assert len(raws) == 1


def test_subfolders_work_and_unsupported_files_are_rejected_not_processed(tmp_path):
    p = make_personal(tmp_path)
    p.drop("프로젝트/a.md", "# A\n\n내용 a")
    bad = p.drop("tool.exe", b"MZ")
    p.settle()
    assert len(articles(p)) == 1
    assert not bad.exists() and list((p.home / "inbox" / "_rejected").rglob("tool.exe"))
    assert p.rt.worker.status()["failed"] == 1


def test_duplicate_content_is_skipped(tmp_path):
    p = make_personal(tmp_path)
    p.drop("a.md", DOC)
    p.settle()
    p.drop("b.md", DOC)
    p.settle()
    assert len(articles(p)) == 1 and not (p.home / "inbox" / "b.md").exists()


def test_temp_and_reserved_names_are_not_scanned(tmp_path):
    p = make_personal(tmp_path)
    p.drop("~$lock.docx", b"x")
    p.drop(".upload-1.part", b"x")
    p.drop("_done/old.md", b"# old")
    p.drop("ok.md", b"# ok")
    assert [x.name for x in scan_inbox(p.rt.settings)] == ["ok.md"]


def test_browser_upload_goes_through_the_same_path_and_needs_no_space_choice(tmp_path):
    p = make_personal(tmp_path)
    c = p.client()
    c.enter()
    r = c.request("POST", "/api/upload", query="space=personal&filename=up.md", body=DOC.encode(),
                  ctype="application/octet-stream", headers={"X-CSRF-Token": c.csrf})
    assert r.status == 201
    assert c.request("POST", "/api/upload", query="space=dept-a&filename=up.md", body=b"x",
                     ctype="application/octet-stream", headers={"X-CSRF-Token": c.csrf}).status == 403
    p.settle()
    assert len(articles(p)) == 1


def test_question_answered_with_citation_from_own_documents(tmp_path):
    p = make_personal(tmp_path)
    p.drop("회의.md", DOC)
    p.settle()
    c = p.client()
    c.enter()
    r = c.post("/api/query", {"question": "서버 증설은 언제?"})
    assert r.status == 200
    body = r.json()
    assert body["answer"].startswith("답변입니다") and len(body["citations"]) == 1
    art = c.get("/api/article", query="path=" + body["citations"][0]["path"])
    assert art.status == 200
    # the model only ever saw the one allowed context
    assert any("QUESTION:" in prompt for _s, prompt in p.llm.calls)


def test_status_endpoint_counts_only(tmp_path):
    p = make_personal(tmp_path)
    c = p.client()
    c.enter()
    assert c.get("/api/personal/status").json() == {"pending": 0, "failed": 0, "working": False, "busy": False}
    p.drop("secret-name.md", DOC)
    p.rt.worker.min_age = 0.0
    p.rt.worker.scan_once()
    p.rt.worker.scan_once()
    st = c.get("/api/personal/status")
    assert st.json()["pending"] == 1 and st.json()["working"] is True
    assert "secret-name" not in st.body.decode() and set(st.json()) == {"pending", "failed", "working", "busy"}
    p.rt.worker.run_once()
    assert c.get("/api/personal/status").json()["pending"] == 0
    assert c.get("/api/personal/status", cookies=False).status == 401


def test_info_endpoint_lists_home_and_watch_folders(tmp_path):
    watch = tmp_path / "Docs"
    watch.mkdir()
    p = make_personal(tmp_path, {"WIKI_PERSONAL_WATCH": str(watch)})
    c = p.client()
    c.enter()
    info = c.get("/api/personal/info").json()
    assert info["home"] == str(p.home) and info["watch"] == [str(watch)] and info["wiki"].endswith("wiki")


def test_watch_folder_end_to_end_original_untouched(tmp_path):
    watch = tmp_path / "Docs"
    watch.mkdir()
    orig = watch / "회의록.md"
    orig.write_text(DOC, encoding="utf-8")
    p = make_personal(tmp_path, {"WIKI_PERSONAL_WATCH": json.dumps([str(watch)])})
    watcher = p.rt.worker.watcher
    watcher.min_age = 0.0
    before = (orig.read_bytes(), orig.stat().st_mtime_ns)
    p.settle()  # scan: stage the copy
    p.settle()  # scan: pick up the staged copy and process it
    assert len(articles(p)) == 1
    assert (orig.read_bytes(), orig.stat().st_mtime_ns) == before and orig.exists()
    assert list((p.home / "inbox" / "_done" / "personal").rglob("*.md"))  # the COPY was archived


def test_pii_masking_applies_to_the_stored_copy_not_the_original(tmp_path):
    watch = tmp_path / "Docs"
    watch.mkdir()
    secret = "010-1234-5678"
    orig = watch / "연락처.md"
    orig.write_text(f"# 연락처\n\n담당자 전화 {secret}\n", encoding="utf-8")
    p = make_personal(tmp_path, {"WIKI_PERSONAL_WATCH": str(watch), "WIKI_MASK_PII": "1"})
    p.rt.worker.watcher.min_age = 0.0
    p.settle()
    p.settle()
    assert secret in orig.read_text(encoding="utf-8")  # the original stays unmasked on the user's disk
    stored = " ".join(f.read_text(encoding="utf-8") for f in (p.home / "raw").rglob("*.md"))
    stored += " ".join(f.read_text(encoding="utf-8") for f in (p.home / "wiki").rglob("*.md"))
    assert secret not in stored and "[PHONE]" in stored
    assert not list((p.home / "inbox" / "_done").rglob("*.md"))  # no unmasked copy is kept either
    assert not list((p.home / "inbox" / "watch").rglob("연락처.md"))


def test_worker_thread_processes_and_stops_cleanly(tmp_path):
    p = make_personal(tmp_path)
    w = p.rt.worker
    w.interval, w.min_age = 0.05, 0.0
    p.drop("t.md", DOC)
    w.start()
    import time
    deadline = time.time() + 10
    while time.time() < deadline and not articles(p):
        time.sleep(0.05)
    w.stop(5)
    assert len(articles(p)) == 1
    p.rt.close()


def test_original_in_watch_folder_is_not_masked_removed_or_renamed_in_any_mode(tmp_path):
    watch = tmp_path / "Docs"
    watch.mkdir()
    orig = watch / "x.md"
    orig.write_text("# X\n\n본문 010-9999-8888", encoding="utf-8")
    for mask in ("0", "1"):
        p = make_personal(tmp_path / f"m{mask}", {"WIKI_PERSONAL_WATCH": str(watch), "WIKI_MASK_PII": mask})
        p.rt.worker.watcher.min_age = 0.0
        p.settle()
        p.settle()
        assert orig.exists() and "010-9999-8888" in orig.read_text(encoding="utf-8")
        assert [x.name for x in watch.iterdir()] == ["x.md"]


def test_watch_folder_with_files_still_being_saved_is_rescanned_soon(tmp_path):
    import time
    watch = tmp_path / "Docs"
    watch.mkdir()
    (watch / "fresh.md").write_text("# fresh text")
    p = make_personal(tmp_path, {"WIKI_PERSONAL_WATCH": str(watch)})
    w = p.rt.worker
    w.watcher.min_age = 3600  # "just saved"
    w.scan_once()
    assert w._next_watch - time.monotonic() <= w.interval + 0.5
    w.watcher.min_age = 0.0
    w._next_watch = 0.0
    assert w.scan_once()["watch_staged"] == 1
    assert w._next_watch - time.monotonic() > 30  # nothing left waiting: back to the long interval
