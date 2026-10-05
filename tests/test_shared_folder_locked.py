"""Locked vs access-denied files: short locks are free, permanent ones are capped, denial is a normal failed attempt."""
from __future__ import annotations

from pathlib import Path

import pytest

from llmwiki.pipeline import notes
from llmwiki.pipeline.watch import Watcher, locked_max_scans
from test_pipeline_support import make_env

SHARING = (13, "sharing violation", None, 32)  # (errno, msg, filename, winerror)
DENIED = (13, "access denied", None, 5)


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def lock_open(monkeypatch, name, args):
    real = open

    def fake(file, *a, **k):
        if Path(file).name == name:
            raise PermissionError(*args)
        return real(file, *a, **k)

    monkeypatch.setattr("builtins.open", fake)


def bucket(env, name):
    d = env.settings.data_dir / "inbox" / name
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()) if d.exists() else []


def test_locked_max_scans_env():
    assert locked_max_scans({}) == 60
    assert locked_max_scans({"WIKI_LOCKED_MAX_SCANS": "7"}) == 7
    assert locked_max_scans({"WIKI_LOCKED_MAX_SCANS": "0"}) == 60 == locked_max_scans({"WIKI_LOCKED_MAX_SCANS": "x"})


def test_short_lock_never_fails_and_resets_counter(env, monkeypatch):
    f = env.drop("dept-a", "stuck.md", "# s\n\n값 1")
    w = Watcher(env.settings, env.queue, env.audit, locked_max=5)
    lock_open(monkeypatch, "stuck.md", SHARING)
    for _ in range(4):
        assert w.scan_once()["waiting"] == 1
    monkeypatch.undo()
    assert w.scan_once()["queued"] == 1  # released after 4 scans: counter reset, nothing failed
    assert f.exists() and "ingest_fail" not in env.system_actions()
    lock_open(monkeypatch, "stuck.md", SHARING)
    w2 = w
    for _ in range(4):
        w2.scan_once()  # a fresh lock starts again at 1
    assert f.exists() and bucket(env, "_failed") == []


def test_permanent_lock_is_given_up_after_cap_with_note_and_audit(env, monkeypatch):
    f = env.drop("dept-a", "stuck.md", "# s\n\nSECRET-CONTENT")
    w = Watcher(env.settings, env.queue, env.audit, locked_max=5)
    lock_open(monkeypatch, "stuck.md", SHARING)
    for _ in range(4):
        w.scan_once()
    assert f.exists()
    w.scan_once()  # 5th consecutive unchanged-but-locked scan
    assert not f.exists() and bucket(env, "_failed") == ["dept-a/stuck.md", "dept-a/stuck.md.reason.txt"]
    note = (f.parent / "stuck.md.처리결과.txt").read_text(encoding="utf-8")
    assert notes.NO_ACCESS in note and "SECRET" not in note
    ev = [e for e in env.audit.entries() if e[1] == "system:pipeline" and e[2] == "ingest_fail"]
    assert len(ev) == 1 and "SECRET" not in str(ev) and "5 scans" in str(ev)


def test_growing_locked_file_is_not_counted_as_stuck(env, monkeypatch):
    f = env.drop("dept-a", "stuck.md", "# s\n\n1")
    w = Watcher(env.settings, env.queue, env.audit, locked_max=3)
    lock_open(monkeypatch, "stuck.md", SHARING)
    for i in range(10):
        with real_append(f):
            pass
        w.scan_once()
    assert f.exists() and bucket(env, "_failed") == []


class real_append:
    """Grow the file on enter (a copy still in progress)."""

    def __init__(self, f):
        self.f = f

    def __enter__(self):
        import io
        with io.open(self.f, "ab") as fh:
            fh.write(b"x")

    def __exit__(self, *a):
        return False


def test_queued_job_gets_finalized_too(env, monkeypatch):
    f = env.drop("dept-a", "stuck.md", "# s\n\n1")
    w = Watcher(env.settings, env.queue, env.audit, locked_max=3)
    assert w.scan_once()["queued"] == 1  # openable at first
    lock_open(monkeypatch, "stuck.md", SHARING)
    for _ in range(3):
        w.scan_once()
    assert not f.exists() and env.queue.jobs()[0]["status"] == "failed"
    assert env.queue.run_pending(lambda p, s: pytest.fail("must not run")) == []


def test_access_denied_is_a_normal_failed_attempt_three_strikes(env, monkeypatch):
    f = env.drop("dept-a", "denied.md", "# d\n\n값 2")
    orig = Path.read_bytes

    def deny(self):
        if self.name == "denied.md":
            raise PermissionError(*DENIED)
        return orig(self)

    monkeypatch.setattr(Path, "read_bytes", deny)
    res = env.run_all()
    assert {r.status for r in res} == {"failed"} and len(res) == env.queue.max_attempts
    job = env.queue.jobs()[0]
    assert job["status"] == "failed" and job["attempts"] == env.queue.max_attempts
    assert bucket(env, "_failed") == ["dept-a/denied.md", "dept-a/denied.md.reason.txt"] and not f.exists()
    assert notes.NO_ACCESS in (f.parent / "denied.md.처리결과.txt").read_text(encoding="utf-8")


def test_access_denied_at_watcher_open_is_queued_not_waited(env, monkeypatch):
    env.drop("dept-a", "denied.md", "# d\n\n1")
    lock_open(monkeypatch, "denied.md", DENIED)
    assert env.watcher.scan_once() == {"queued": 1, "rejected": 0, "waiting": 0}
