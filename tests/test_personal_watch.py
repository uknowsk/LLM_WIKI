"""Watch folders: originals are only read; new/changed files are copied to staging; junk, links and loops are ignored."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from llmwiki.personal import watchfolders
from llmwiki.personal.watchfolders import WatchIndex, WatchScanner


@pytest.fixture
def env(tmp_path):
    home, watch = tmp_path / "home", tmp_path / "Documents"
    (home / "inbox").mkdir(parents=True)
    watch.mkdir()
    index = WatchIndex(home / "watch_index.db")
    yield home, watch, index
    index.close()


def scanner(env, **kw):
    home, watch, index = env
    kw.setdefault("min_age", 0.0)
    return WatchScanner(home, [watch], index, **kw)


def staged(home):
    return sorted(p for p in (home / "inbox" / "watch").rglob("*") if p.is_file())


def snapshot(p: Path):
    st = p.stat()
    return p.read_bytes(), st.st_size, st.st_mtime_ns


def test_new_file_is_copied_and_original_untouched(env):
    home, watch, _ = env
    doc = watch / "회의록" / "2026-10-01 주간.md"
    doc.parent.mkdir()
    doc.write_text("# 주간 회의\n\n결정 사항", encoding="utf-8")
    before = snapshot(doc)
    assert scanner(env).scan()["staged"] == 1
    [copy] = staged(home)
    assert copy.name == doc.name and copy.read_bytes() == before[0] and copy != doc
    assert snapshot(doc) == before and doc.exists()


def test_unchanged_file_is_not_staged_again_and_changed_file_is_(env):
    home, watch, _ = env
    doc = watch / "a.txt"
    doc.write_text("v1")
    s = scanner(env)
    assert s.scan()["staged"] == 1
    for p in staged(home):
        p.unlink()
    assert s.scan() == {"staged": 0, "unchanged": 1, "waiting": 0, "skipped": 0} and not staged(home)
    doc.write_text("version two")
    assert s.scan()["staged"] == 1
    assert [p.read_bytes() for p in staged(home)] == [b"version two"]


def test_touched_but_identical_content_is_not_restaged(env):
    home, watch, _ = env
    doc = watch / "a.txt"
    doc.write_text("same")
    s = scanner(env)
    s.scan()
    for p in staged(home):
        p.unlink()
    os.utime(doc, ns=(doc.stat().st_atime_ns, doc.stat().st_mtime_ns + 5_000_000_000))
    assert s.scan()["staged"] == 0 and not staged(home)
    assert s.scan()["unchanged"] == 1  # the new signature was remembered


def test_deleting_the_original_removes_nothing(env):
    home, watch, _ = env
    doc = watch / "a.md"
    doc.write_text("# t\n\nx")
    s = scanner(env)
    s.scan()
    doc.unlink()
    s.scan()
    assert len(staged(home)) == 1


def test_only_supported_plausible_files(env):
    home, watch, _ = env
    for name in ("ok.md", "ok.txt", "ok.docx", "ok.xlsx", "ok.eml", "ok.pdf", "UP.PDF"):
        (watch / name).write_bytes(b"data " + name.encode())
    for name in ("~$lock.docx", "Thumbs.db", "desktop.ini", "x.tmp", "x.part", ".hidden.md", "app.exe", "pic.png",
                 "a.lnk", "noext", "x.md.crdownload"):
        (watch / name).write_bytes(b"junk")
    (watch / "empty.md").write_bytes(b"")
    scanner(env).scan()
    assert sorted(p.name for p in staged(home)) == ["UP.PDF", "ok.docx", "ok.eml", "ok.md", "ok.pdf", "ok.txt", "ok.xlsx"]


@pytest.mark.skipif(os.name != "nt", reason="hidden/system attributes are a Windows feature")
def test_hidden_and_system_attribute_files_and_folders_are_ignored(env):
    home, watch, _ = env
    (watch / "visible.md").write_text("v")
    (watch / "h.md").write_text("h")
    (watch / "s.md").write_text("s")
    d = watch / "hiddendir"
    d.mkdir()
    (d / "in.md").write_text("in")
    for p, flag in ((watch / "h.md", "+h"), (watch / "s.md", "+s"), (d, "+h")):
        subprocess.run(["attrib", flag, str(p)], check=True, capture_output=True)
    scanner(env).scan()
    assert [p.name for p in staged(home)] == ["visible.md"]


def test_size_cap(env):
    home, watch, _ = env
    (watch / "small.md").write_bytes(b"x" * 10)
    (watch / "big.md").write_bytes(b"x" * 100)
    counts = scanner(env, max_bytes=50).scan()
    assert [p.name for p in staged(home)] == ["small.md"] and counts["skipped"] == 1


def test_recent_files_wait_for_min_age(env):
    home, watch, _ = env
    (watch / "fresh.md").write_text("just saved")
    s = scanner(env, min_age=3600)
    assert s.scan()["waiting"] == 1 and not staged(home)
    s.min_age = 0
    assert s.scan()["staged"] == 1


def test_staging_backpressure(env):
    home, watch, _ = env
    for i in range(5):
        (watch / f"d{i}.md").write_text(f"doc {i}")
    s = scanner(env, max_pending=2)
    assert s.scan()["staged"] == 2
    assert s.scan()["staged"] == 0  # staging folder is full until the worker has consumed it
    for p in staged(home):
        p.unlink()
    assert s.scan()["staged"] == 2


def test_nothing_inside_the_data_home_is_ever_scanned(tmp_path):
    root = tmp_path / "Documents"  # the watch folder CONTAINS the data home
    home = root / "LLMWiki"
    (home / "inbox").mkdir(parents=True)
    (home / "wiki").mkdir()
    (home / "wiki" / "a.md").write_text("article")
    (home / "inbox" / "_done").mkdir()
    (home / "inbox" / "_done" / "b.md").write_text("done")
    (root / "real.md").write_text("real")
    index = WatchIndex(home / "watch_index.db")
    try:
        WatchScanner(home, [root], index, min_age=0).scan()
        assert [p.name for p in staged(home)] == ["real.md"]
        # a watch folder that is the home itself or inside it is dropped altogether
        s = WatchScanner(home, [home, home / "wiki"], index, min_age=0)
        assert s.usable_roots() == [] and s.scan()["staged"] == 0
    finally:
        index.close()


def test_missing_or_file_roots_are_ignored(env):
    home, watch, index = env
    f = watch / "afile.md"
    f.write_text("x")
    s = WatchScanner(home, [watch / "nope", f], index, min_age=0)
    assert s.usable_roots() == [] and s.scan()["staged"] == 0


def test_file_changing_during_copy_is_not_staged(env, monkeypatch):
    home, watch, _ = env
    doc = watch / "a.md"
    doc.write_text("abc")
    real_stat = os.lstat

    def fake_stat(path, *a, **kw):  # the post-copy re-check sees a different mtime
        st = real_stat(path, *a, **kw)
        if str(path) == str(doc):
            fields = list(st)
            fields[8] += 5
            return os.stat_result(fields)
        return st

    s = scanner(env)
    monkeypatch.setattr(watchfolders.os, "lstat", fake_stat)
    assert s.scan()["staged"] == 0
    monkeypatch.undo()
    assert not staged(home) and not list((home / "inbox" / "watch").glob(".copy-*"))  # no temp leftovers


def _make_link(link: Path, target: Path, directory: bool) -> bool:
    try:
        os.symlink(target, link, target_is_directory=directory)
        return True
    except (OSError, NotImplementedError):
        pass
    if directory and os.name == "nt":  # a junction needs no privilege
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return r.returncode == 0
    return False


def test_links_to_files_and_folders_are_never_followed(tmp_path, env):
    home, watch, _ = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("outside the watched folder")
    (watch / "real.md").write_text("real")
    made_dir = _make_link(watch / "linkdir", outside, True)
    made_file = _make_link(watch / "linkfile.md", outside / "secret.md", False)
    if not (made_dir or made_file):
        pytest.skip("cannot create symlinks/junctions here")
    scanner(env).scan()
    names = [p.name for p in staged(home)]
    assert names == ["real.md"], names
    assert (outside / "secret.md").read_text() == "outside the watched folder"


def test_index_survives_restart(env):
    home, watch, index = env
    (watch / "a.md").write_text("x")
    scanner(env).scan()
    index.close()
    index2 = WatchIndex(home / "watch_index.db")
    try:
        assert WatchScanner(home, [watch], index2, min_age=0).scan()["unchanged"] == 1
    finally:
        index2.close()
