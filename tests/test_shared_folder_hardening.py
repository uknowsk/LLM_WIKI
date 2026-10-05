"""Review findings on the shared-folder intake: depth cap, long names, reserved case, nested aliases, forged job space,
error-text leakage, marker overwrite, given-up loop (synthetic data, test-first)."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from llmwiki.engine.llm import FakeLLM
from llmwiki.pipeline import inbox, notes
from llmwiki.pipeline import watch as watch_mod
from llmwiki.pipeline.aliases import AliasError, parse_aliases
from test_pipeline_support import make_env, make_eml


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def bucket(env, name):
    d = env.settings.data_dir / "inbox" / name
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()) if d.exists() else []


def deny(*a, **k):
    raise PermissionError(13, "denied")


# ---- 1. deep nesting ----------------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="uses long path prefix")
def test_deep_nesting_does_not_crash_scan_and_is_rejected_once(env):
    n = 1100
    base = env.settings.data_dir / "inbox" / "dept-a"
    base.mkdir(parents=True)
    deep = "\\\\?\\" + str(base) + "\\d" * n
    try:
        for i in range(1, n + 1):  # os.makedirs itself recurses
            os.mkdir("\\\\?\\" + str(base) + "\\d" * i)
    except OSError:
        pytest.skip("OS refuses such a deep tree")
    try:
        c1 = env.watcher.scan_once()
        assert c1["rejected"] == 1 and c1["queued"] == 0
        assert env.watcher.scan_once()["rejected"] == 0  # reported once
        found = list(base.rglob("*.처리결과.txt"))
        assert len(found) == 1 and notes.LOCATION in found[0].read_text(encoding="utf-8")
    finally:
        for i in range(n, 0, -1):
            try:
                os.rmdir("\\\\?\\" + str(base) + "\\d" * i)
            except OSError:
                break


def test_run_forever_survives_a_failing_iteration(env, monkeypatch, caplog):
    calls = []

    def boom():
        calls.append(1)
        if len(calls) == 1:
            raise RecursionError("SECRET-NAME")

    monkeypatch.setattr(env.watcher, "scan_once", boom)
    sleeps = []
    monkeypatch.setattr(watch_mod.time, "sleep", lambda s: sleeps.append(s))
    env.watcher.run_forever(lambda p, s: None, interval=1.0, should_stop=lambda: len(calls) >= 3)
    assert len(calls) == 3 and sleeps and max(sleeps) > 1.0  # backed off after the failure
    assert any("RecursionError" in r.getMessage() for r in caplog.records)
    assert not any("SECRET" in r.getMessage() for r in caplog.records)


# ---- 2. long names -------------------------------------------------------------------------------------------
@pytest.fixture
def lenv(tmp_path):
    """Long-path capable data dir so a 250 character file name (limit 255 per component) can exist at all."""
    if sys.platform != "win32":
        pytest.skip("Windows long path prefix")
    return make_env(Path("\\\\?\\" + str(tmp_path)))


def test_long_name_rejected_at_scan_without_crash(lenv):
    try:
        lenv.drop("dept-a", "a" * 246 + ".hwp", b"1")
    except OSError:
        pytest.skip("cannot create long names")
    assert lenv.watcher.scan_once()["rejected"] == 1
    moved = [p for p in (lenv.settings.data_dir / "inbox" / "_rejected").rglob("*") if p.is_file()]
    assert moved and all(len(p.name) <= 240 for p in moved)


def test_long_name_rejected_in_process_file_without_crash(lenv):
    f = lenv.drop("dept-a", "b" * 236 + ".hwp", b"1")
    assert lenv.process(f, "dept-a").status == "rejected"
    assert all(len(p.name) <= 240 for p in (lenv.settings.data_dir / "inbox" / "_rejected").rglob("*"))


def test_long_name_final_failure_does_not_escape_run_pending(lenv):
    f = lenv.drop("dept-a", "c" * 235 + ".docx", b"not a zip")
    lenv.queue.enqueue(f, "dept-a")  # a stale job: the scan itself would have rejected such a name
    lenv.queue.drain(lambda p, s: lenv.process(p, s))  # must not raise
    assert lenv.queue.jobs()[0]["status"] == "failed"
    assert all(len(p.name) <= 240 for p in (lenv.settings.data_dir / "inbox" / "_failed").rglob("*"))


def test_safe_dest_name_truncates_keeping_extension():
    out = inbox._safe_dest_name("가" * 300 + ".docx")
    assert len(out) <= 220 and out.endswith(".docx")
    assert inbox.bad_filename("x" * 201) and not inbox.bad_filename("x" * 200)


# ---- 3. reserved folders ------------------------------------------------------------------------------------
@pytest.mark.parametrize("folder", ["_Done", "_DONE", "_rejected", "_Failed", "_Foo", "_misc"])
def test_underscore_root_folders_are_reserved_case_insensitively(env, folder):
    env.drop(f"{folder}/dept-a", "x.md", "# x\n\n1")
    assert inbox.scan(env.settings) == []
    assert env.watcher.scan_once() == {"queued": 0, "rejected": 0, "waiting": 0}
    assert env.system_actions() == []


def test_inside_inbox_and_notice_folder_use_underscore_rule(env):
    root = env.settings.data_dir / "inbox"
    root.mkdir(parents=True)
    assert inbox._inside_inbox(env.settings, root / "_Foo" / "x.md") is None
    assert inbox.notify(env.settings, root / "_Foo" / "x.md", notes.FAILED) is None


# ---- 4. nested aliases ---------------------------------------------------------------------------------------
def test_nested_alias_under_other_department_refused():
    with pytest.raises(AliasError, match="상위"):
        parse_aliases(json.dumps({"인사팀": "dept-hr", "인사팀/당직": "dept-a/part-1"}, ensure_ascii=False).encode())
    with pytest.raises(AliasError):  # case-insensitive prefix, 3 levels
        parse_aliases(json.dumps({"Hr팀": "dept-hr", "hr팀/a/b": "dept-a"}).encode())


def test_nested_alias_refinement_of_same_department_allowed():
    raw = {"인프라팀": "dept-a", "인프라팀/당직": "dept-a/part-1", "인프라팀/당직/야간": "dept-a/part-1/n"}
    a = parse_aliases(json.dumps(raw, ensure_ascii=False).encode())
    assert a.resolve(["인프라팀", "당직"]) == "dept-a/part-1"
    same = parse_aliases(json.dumps({"인프라팀": "dept-a", "인프라팀/당직": "dept-a"}, ensure_ascii=False).encode())
    assert same.resolve(["인프라팀", "당직"]) == "dept-a"


# ---- 5. forged / stale job space ------------------------------------------------------------------------------
def test_forged_job_space_is_rejected_without_move_or_ingest(env):
    f = env.drop("dept-a", "secret-name.md", "# s\n\n내부 자료")
    env.queue.enqueue(f, "dept-b")  # forged row: the folder says dept-a
    res = env.queue.drain(lambda p, s: env.process(p, s))
    assert [r.status for r in res] == ["rejected"]
    assert f.exists() and env.store.article_paths() == []
    assert not (env.settings.raw_dir.exists() and any(env.settings.raw_dir.rglob("*.md")))
    ev = [e for e in env.audit.entries() if e[2] == "ingest_reject"]
    assert ev and not any("secret-name" in str(e) for e in ev)


def test_alias_change_after_enqueue_is_caught(env):
    f = env.drop("인사팀", "a.md", "# a\n\n1")
    env.queue.enqueue(f, "dept-hr")
    changed = parse_aliases('{"인사팀": "dept-x"}'.encode())
    assert env.process(f, "dept-hr", aliases=changed).status == "rejected" and f.exists()
    assert env.process(f, "dept-x", aliases=changed).status == "done"


def test_startup_drops_pending_jobs(env):
    f = env.drop("dept-a", "a.md", "# a\n\n1")
    env.queue.enqueue(f, "dept-b")
    env.queue.drop_pending()
    assert env.queue.jobs() == []


# ---- 6. no raw exception text ---------------------------------------------------------------------------------
def test_error_text_never_reaches_last_error_audit_or_reason_file(tmp_path):
    def leaky(system, prompt):
        raise RuntimeError("echo SECRET-FRAGMENT-123 of the document")

    env = make_env(tmp_path, llm=FakeLLM(leaky))
    env.drop("dept-a", "a.md", "# a\n\n본문 내용")
    env.run_all()
    job = env.queue.jobs()[0]
    assert "SECRET" not in job["last_error"] and "RuntimeError" in job["last_error"] and notes.FAILED in job["last_error"]
    assert not any("SECRET" in str(e) for e in env.audit.entries())
    for p in (env.settings.data_dir / "inbox").rglob("*.txt"):
        assert "SECRET" not in p.read_text(encoding="utf-8")
    assert any(p.name.endswith(".reason.txt") for p in (env.settings.data_dir / "inbox" / "_failed").rglob("*"))


def test_attachment_error_text_is_sanitised(env):
    env.drop("dept-a", "m.eml", make_eml("s", "본문 123", [("bad.docx", b"SECRET-ATTACH not a zip")]))
    res = env.run_all()
    assert res[0].status == "done" and res[0].attachment_errors
    assert not any("SECRET" in e for e in res[0].attachment_errors)
    assert not any("SECRET" in str(e) for e in env.audit.entries())


# ---- 7. markers and the given-up loop --------------------------------------------------------------------------
def test_tombstone_never_overwrites_earlier_marker(tmp_path):
    env = make_env(tmp_path, mask=True)
    env.drop("dept-a", "same.md", "# 하나\n\n전화 010-1111-2222 값 1")
    env.run_all()
    env.drop("dept-a", "same.md", "# 둘\n\n전화 010-3333-4444 값 2")
    env.run_all()
    assert bucket(env, "_done") == ["dept-a/same.md.processed-2.txt", "dept-a/same.md.processed.txt"]
    texts = {p.read_text(encoding="utf-8") for p in (env.settings.data_dir / "inbox" / "_done").rglob("*.txt")}
    assert len(texts) == 2  # two different sha256 lines survive


def test_unmovable_given_up_file_is_not_requeued_until_it_changes(env, monkeypatch):
    f = env.drop("dept-a", "bad.docx", b"not a zip")
    monkeypatch.setattr(shutil, "move", deny)
    env.run_all()  # attempts exhausted, move impossible -> the file stays
    assert f.exists() and env.queue.jobs()[0]["attempts"] == env.queue.max_attempts
    for _ in range(3):
        assert env.watcher.scan_once()["queued"] == 0
    assert env.queue.jobs()[0]["attempts"] == env.queue.max_attempts and env.queue.jobs()[0]["status"] == "failed"
    with open(f, "ab") as fh:
        fh.write(b"fixed")
    assert env.watcher.scan_once()["queued"] == 1


def test_unmovable_reject_does_not_spam(env, monkeypatch):
    f = env.drop("dept-a", "x.hwp", b"1")
    monkeypatch.setattr(shutil, "move", deny)
    env.run_all()
    n = len(env.system_actions())
    for _ in range(3):
        env.watcher.scan_once()
        env.queue.run_pending(lambda p, s: env.process(p, s))
    assert f.exists() and len(env.system_actions()) == n
