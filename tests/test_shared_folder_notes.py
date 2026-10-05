"""Worker feedback notes (`<name>.처리결과.txt`): written on reject / final failure, never leak, removed on success."""
from __future__ import annotations

import pytest

from llmwiki.pipeline import inbox, notes
from test_pipeline_support import make_env


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def note_files(env, space="dept-a"):
    d = env.settings.data_dir / "inbox" / space
    return sorted(p.name for p in d.glob("*.처리결과*.txt"))


def test_unsupported_type_writes_note_with_category_time_and_action(env):
    env.drop("dept-a", "구형.hwp", b"\x00hwp")
    env.run_all()
    assert note_files(env) == ["구형.hwp.처리결과.txt"]
    text = (env.settings.data_dir / "inbox" / "dept-a" / "구형.hwp.처리결과.txt").read_text(encoding="utf-8")
    assert notes.UNSUPPORTED in text and "시각:" in text and "조치:" in text and "docx" in text


def test_corrupt_file_note_after_final_failure_and_no_leak(env):
    secret = "SECRET-BODY-9999"
    env.drop("dept-a", "깨짐.docx", ("not a zip " + secret).encode())
    env.run_all()
    assert note_files(env) == ["깨짐.docx.처리결과.txt"]  # only after the attempts are exhausted
    text = (env.settings.data_dir / "inbox" / "dept-a" / "깨짐.docx.처리결과.txt").read_text(encoding="utf-8")
    assert notes.CORRUPT in text
    assert secret not in text and "Error" not in text and "docx:" not in text and "zip" not in text  # no raw exception text
    assert [p.name for p in (env.settings.data_dir / "inbox" / "_failed" / "dept-a").glob("*.docx")] == ["깨짐.docx"]


def test_generic_failure_category(env):
    env.drop("dept-a", "d.md", "# 다운\n\nLLMDOWN 444")
    env.run_all()
    text = (env.settings.data_dir / "inbox" / "dept-a" / "d.md.처리결과.txt").read_text(encoding="utf-8")
    assert notes.FAILED in text and "endpoint down" not in text and "LLMDOWN" not in text


@pytest.mark.parametrize("err,cat", [
    ("unsupported file type: .hwp", notes.UNSUPPORTED), ("ValueError: not a valid .docx: x.docx", notes.CORRUPT),
    ("ValueError: malformed eml: no mail headers", notes.CORRUPT), ("PermissionError: [Errno 13] x", notes.NO_ACCESS),
    ("ValueError: no extractable text (scanned PDF without OCR?)", notes.UNREADABLE), ("LLMError: boom", notes.FAILED),
    ("file too large (> 5 bytes)", notes.TOO_LARGE), ("", notes.FAILED)])
def test_categorize_error(err, cat):
    assert notes.categorize_error(err) == cat


def test_existing_note_is_never_overwritten(env):
    folder = env.settings.data_dir / "inbox" / "dept-a"
    folder.mkdir(parents=True)
    (folder / "x.hwp.처리결과.txt").write_text("관리자 메모", encoding="utf-8")
    env.drop("dept-a", "x.hwp", b"1")
    env.run_all()
    assert (folder / "x.hwp.처리결과.txt").read_text(encoding="utf-8") == "관리자 메모"
    assert note_files(env) == ["x.hwp.처리결과-2.txt", "x.hwp.처리결과.txt"]
    for _ in range(12):  # capped, never raises, never overwrites
        notes.write_note(folder, "x.hwp", notes.FAILED)
    assert len(note_files(env)) == notes.MAX_NOTES


def test_notes_are_not_scanned_queued_or_rejected(env):
    env.drop("dept-a", "x.hwp", b"1")
    env.run_all()
    before = note_files(env)
    c = env.watcher.scan_once()
    assert c == {"queued": 0, "rejected": 0, "waiting": 0} and note_files(env) == before
    assert not any(i.path.name.endswith(".txt") for i in inbox.scan(env.settings))


def test_stale_note_removed_after_later_success(env):
    f = env.drop("dept-a", "r.md", "# r\n\nLLMDOWN 1")
    env.run_all()
    assert note_files(env) == ["r.md.처리결과.txt"]
    env.drop("dept-a", "r.md", "# r\n\n값 12")  # fixed and dropped again under the same name
    assert env.run_all()[-1].status == "done"
    assert note_files(env) == [] and not f.exists()


def test_stale_note_removed_for_duplicate_skip(env):
    env.drop("dept-a", "one.md", "# 보고\n\n금액 500")
    env.run_all()
    folder = env.settings.data_dir / "inbox" / "dept-a"
    notes.write_note(folder, "copy.md", notes.FAILED)
    env.drop("dept-a", "copy.md", "# 보고\n\n금액 500")
    assert env.run_all()[-1].status == "skipped" and note_files(env) == []


def test_note_write_failure_never_fails_the_pipeline(env, monkeypatch, caplog):
    real = open

    def fake_open(file, mode="r", *a, **k):
        if str(file).endswith(".처리결과.txt") and "x" in mode:
            raise PermissionError(13, "denied C:\\secret\\path")
        return real(file, mode, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    env.drop("dept-a", "x.hwp", b"1")
    res = env.run_all()
    assert res[0].status == "rejected" and note_files(env) == []
    assert any("not written" in r.getMessage() for r in caplog.records)
    assert not any("secret" in r.getMessage() or "x.hwp" in r.getMessage() for r in caplog.records)  # no path/name in logs


def test_note_failure_on_clear_is_swallowed(env, monkeypatch, tmp_path):
    folder = env.settings.data_dir / "inbox" / "dept-a"
    folder.mkdir(parents=True)
    (folder / "a.md.처리결과.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "no")))
    assert notes.clear_notes(folder, "a.md") == 0


def test_no_note_for_root_level_reserved_or_outside_files(env, tmp_path):
    env.drop("", "loose.hwp", b"1")
    env.run_all()
    assert not list((env.settings.data_dir / "inbox").glob("*.처리결과*"))
    assert inbox.notify(env.settings, tmp_path / "elsewhere" / "a.hwp", notes.FAILED) is None
    assert inbox.notify(env.settings, env.settings.data_dir / "inbox" / "_done" / "a.hwp", notes.FAILED) is None


def test_note_goes_to_the_same_alias_folder_the_file_came_from(env):
    from llmwiki.pipeline.aliases import parse_aliases
    from llmwiki.pipeline.watch import Watcher

    aliases = parse_aliases('{"인사팀": "dept-hr"}'.encode())
    w = Watcher(env.settings, env.queue, env.audit, aliases=aliases)
    env.drop("인사팀", "표.hwp", b"1")
    w.scan_once()
    env.queue.drain(lambda p, s: env.process(p, s, aliases=aliases))
    assert (env.settings.data_dir / "inbox" / "인사팀" / "표.hwp.처리결과.txt").is_file()
    assert (env.settings.data_dir / "inbox" / "_rejected" / "dept-hr" / "표.hwp").is_file()  # canonical layout
