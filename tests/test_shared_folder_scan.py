"""Shared network folder intake: housekeeping files, locked/growing files, links, odd names, size cap (synthetic data)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llmwiki.pipeline import inbox, notes
from llmwiki.pipeline.watch import Watcher
from test_pipeline_support import make_env


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def names(items):
    return sorted(i.path.name for i in items)


def bucket(env, name):
    d = env.settings.data_dir / "inbox" / name
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()) if d.exists() else []


# ---- 1. housekeeping files are ignored silently ------------------------------------------------------
@pytest.mark.parametrize("junk", ["~$보고서.docx", "~$a.xlsx", "Thumbs.db", "thumbs.db", "desktop.ini", ".DS_Store",
                                  "바로가기.lnk", "x.md.tmp", "x.part", "큰파일.crdownload", ".hidden.md", "a.md.처리결과.txt",
                                  "a.md.처리결과-2.txt"])
def test_housekeeping_names_ignored_silently(env, junk):
    env.drop("dept-a", junk, "x")
    env.drop("dept-a", "real.md", "# real\n\n값 1")
    items = inbox.scan(env.settings)
    assert names(items) == ["real.md"]
    counts = env.watcher.scan_once()
    assert counts == {"queued": 1, "rejected": 0, "waiting": 0}
    assert bucket(env, "_rejected") == [] and env.system_actions() == []


def test_housekeeping_at_inbox_root_is_not_rejected(env):
    env.drop("", "~$lock.docx", "x")
    env.drop("", "Thumbs.db", "x")
    assert env.watcher.scan_once() == {"queued": 0, "rejected": 0, "waiting": 0}
    assert bucket(env, "_rejected") == [] and env.system_actions() == []


def test_nfd_note_name_is_still_recognised():
    import unicodedata
    assert notes.is_note_name(unicodedata.normalize("NFD", "a.md.처리결과.txt"))


def test_hidden_and_system_attribute_ignored(env, monkeypatch):
    env.drop("dept-a", "secret.md", "# s\n\n1")
    env.drop("dept-a", "sys.md", "# s\n\n2")
    env.drop("dept-a", "ok.md", "# o\n\n3")
    env.drop("$RECYCLE.BIN", "x.md", "# o\n\n3")
    hidden = {"secret.md": inbox.FILE_ATTRIBUTE_HIDDEN, "sys.md": inbox.FILE_ATTRIBUTE_SYSTEM}
    real = inbox._attrs
    monkeypatch.setattr(inbox, "_attrs", lambda e: hidden.get(e.name, real(e)))
    assert names(inbox.scan(env.settings)) == ["ok.md"]
    assert env.watcher.scan_once()["rejected"] == 0


def test_hidden_directory_is_not_descended(env, monkeypatch):
    env.drop("dept-a/cache", "x.md", "# x\n\n1")
    real = inbox._attrs
    monkeypatch.setattr(inbox, "_attrs", lambda e: inbox.FILE_ATTRIBUTE_HIDDEN if e.name == "cache" else real(e))
    assert inbox.scan(env.settings) == []


# ---- 4a. locked / growing files are retried, never counted as a failed attempt -----------------------
def test_locked_file_skipped_by_watcher_and_not_failed(env, monkeypatch):
    f = env.drop("dept-a", "locked.md", "# l\n\n값 5")
    real_open = open

    def fake_open(file, *a, **k):
        if Path(file).name == "locked.md":
            raise PermissionError(13, "sharing violation", None, 32)
        return real_open(file, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    assert env.watcher.scan_once() == {"queued": 0, "rejected": 0, "waiting": 1}
    monkeypatch.undo()
    assert env.watcher.scan_once()["queued"] == 1 and f.exists()


def test_locked_during_processing_is_retried_not_failed(env, monkeypatch):
    f = env.drop("dept-a", "busy.md", "# b\n\n값 6")
    env.watcher.scan_once()
    orig = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(PermissionError(13, "in use", None, 32))
                        if self.name == "busy.md" else orig(self))
    res = env.queue.drain(lambda p, s: env.process(p, s))
    assert {r.status for r in res} == {"locked"}
    job = env.queue.jobs()[0]
    assert job["status"] == "new" and job["attempts"] == 0 and f.exists()
    assert "ingest_fail" not in env.system_actions() and bucket(env, "_failed") == []
    monkeypatch.undo()
    assert env.queue.drain(lambda p, s: env.process(p, s))[0].status == "done"
    assert not f.exists()


def test_growing_file_waits_until_stable_across_two_scans(env):
    f = env.drop("dept-a", "grow.md", "# g\n\n" + "x" * 10)
    old = time.time() - 3600
    os.utime(f, (old, old))
    w = Watcher(env.settings, env.queue, env.audit, min_age=1.0)
    assert w.scan_once()["queued"] == 0  # first sight: no previous signature
    f.write_text("# g\n\n" + "x" * 500, encoding="utf-8")
    os.utime(f, (old, old))
    assert w.scan_once()["queued"] == 0  # size changed since last scan
    assert w.scan_once()["queued"] == 1  # unchanged across two scans


def test_file_changing_while_read_is_locked_not_failed(env, monkeypatch):
    f = env.drop("dept-a", "w.md", "# w\n\n값 7")
    orig = Path.read_bytes

    def grow(self):
        data = orig(self)
        if self.name == "w.md":
            with open(self, "ab") as fh:
                fh.write(b"more")
        return data

    monkeypatch.setattr(Path, "read_bytes", grow)
    assert env.process(f, "dept-a").status == "locked"
    assert f.exists() and bucket(env, "_failed") == []


# ---- 4b. links / reparse points -----------------------------------------------------------------------
def test_reparse_attribute_directory_reported_not_followed(env, monkeypatch):
    env.drop("dept-b", "secret.md", "# s\n\n기밀")
    (env.settings.data_dir / "inbox" / "dept-a").mkdir()
    (env.settings.data_dir / "inbox" / "dept-a" / "alias").mkdir()
    (env.settings.data_dir / "inbox" / "dept-a" / "alias" / "inside.md").write_text("# i\n\n1", encoding="utf-8")
    real = inbox._attrs
    monkeypatch.setattr(inbox, "_attrs",
                        lambda e: inbox.FILE_ATTRIBUTE_REPARSE_POINT if e.name == "alias" else real(e))
    items = inbox.scan(env.settings)
    linked = [i for i in items if i.is_dir]
    assert [i.path.name for i in linked] == ["alias"] and linked[0].space is None
    assert not any(i.path.name == "inside.md" for i in items)  # not descended
    c1 = env.watcher.scan_once()
    assert c1["rejected"] == 1 and c1["queued"] == 1  # dept-b/secret.md is a normal file of its own space
    assert env.watcher.scan_once()["rejected"] == 0  # reported once, not every scan
    assert (env.settings.data_dir / "inbox" / "dept-a" / "alias" / "inside.md").exists()  # nothing moved
    assert (env.settings.data_dir / "inbox" / "dept-a" / "alias.처리결과.txt").exists()
    assert "ingest_reject" in env.system_actions()


def test_real_junction_to_other_space_is_not_followed(env, tmp_path):
    if sys.platform != "win32":
        pytest.skip("junctions are Windows only")
    inb = env.settings.data_dir / "inbox"
    env.drop("dept-b", "secret.md", "# s\n\n기밀 예산")
    (inb / "dept-a").mkdir()
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(inb / "dept-a" / "peek"), str(inb / "dept-b")],
                       capture_output=True)
    if r.returncode != 0:
        pytest.skip("cannot create a junction here")
    try:
        items = inbox.scan(env.settings)
        assert [(i.path.parent.name, i.path.name, i.space) for i in items if i.path.name != "peek"] == [
            ("dept-b", "secret.md", "dept-b")]
        assert any(i.path.name == "peek" and i.space is None and i.is_dir for i in items)
        assert inbox.has_link_component(env.settings, inb / "dept-a" / "peek" / "secret.md")
        res = env.process(inb / "dept-a" / "peek" / "secret.md", "dept-a")  # a stale/forged job through the link
        assert res.status == "rejected" and (inb / "dept-b" / "secret.md").exists()
        assert env.store.article_paths() == []
    finally:
        os.rmdir(inb / "dept-a" / "peek")  # removes the junction only


def test_symlink_file_is_rejected(env):
    inb = env.settings.data_dir / "inbox"
    env.drop("dept-a", "real.md", "# r\n\n1")
    try:
        os.symlink(inb / "dept-a" / "real.md", inb / "dept-a" / "lnk.md")
    except (OSError, NotImplementedError):
        pytest.skip("no symlink privilege")
    items = {i.path.name: i for i in inbox.scan(env.settings)}
    assert items["lnk.md"].space is None and items["real.md"].space == "dept-a"


# ---- 4c/d. size cap and odd names ----------------------------------------------------------------------
def test_size_cap_enforced_before_reading(env, monkeypatch):
    f = env.drop("dept-a", "huge.md", "# h\n\n" + "x" * 2000)
    monkeypatch.setenv("WIKI_MAX_INPUT_BYTES", "1000")
    reads = []
    orig = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self.name) or orig(self))
    res = env.process(f, "dept-a")
    assert res.status == "rejected" and "too large" in res.error and "huge.md" not in reads
    assert bucket(env, "_rejected") == ["dept-a/huge.md", "dept-a/huge.md.reason.txt"]
    note = (f.parent / "huge.md.처리결과.txt").read_text(encoding="utf-8")
    assert notes.TOO_LARGE in note


@pytest.mark.parametrize("bad", ["a:b.md", "a|b.md", "x?.md", "con.md", "nul.txt", "trail.", "a..b.md", "q*.md", 'q".md',
                                 "a/b.md", "..\\x.md", "tab\t.md"])
def test_bad_file_name_detection(bad):
    assert inbox.bad_filename(bad)


def test_bad_file_name_rejected_with_note_and_never_leaves_folder(env, monkeypatch):
    # Windows cannot create such names itself, so the detection is injected; they come from non-Windows share clients
    monkeypatch.setattr(inbox, "bad_filename", lambda n: n == "weird.md")
    f = env.drop("dept-a", "weird.md", "# b\n\n1")
    counts = env.watcher.scan_once()
    assert counts == {"queued": 0, "rejected": 1, "waiting": 0}
    assert not f.exists() and bucket(env, "_rejected") == ["weird.md", "weird.md.reason.txt"]  # flattened, inside inbox
    assert notes.BAD_NAME in (f.parent / "weird.md.처리결과.txt").read_text(encoding="utf-8")


def test_bad_names_in_scan_and_safe_destination():
    assert inbox.bad_filename("a:b") and not inbox.bad_filename("회의록 (최종)_v2.md")
    assert "/" not in inbox._safe_dest_name("..\\..\\x") and not inbox._safe_dest_name("x.").endswith(".")


# ---- root-level files stay rejected ---------------------------------------------------------------------
def test_root_level_file_still_rejected_without_note(env):
    env.drop("", "loose.md", "# l\n\n1")
    assert env.watcher.scan_once()["rejected"] == 1
    assert bucket(env, "_rejected") == ["loose.md", "loose.md.reason.txt"]
    assert not list((env.settings.data_dir / "inbox").glob("*.처리결과*"))  # names stay invisible to everyone
