import threading
import time

import pytest

from llmwiki.audit import AuditLog
from llmwiki.engine.lint import lint_grounding
from llmwiki.engine.llm import FakeLLM
from llmwiki.engine.query import NO_EVIDENCE, AccessDenied, QueryService
from llmwiki.engine.store import Store
from llmwiki.ingest.pdf import FakeOcrEngine
from llmwiki.pipeline import inbox, notes
from llmwiki.pipeline.queue import JobQueue
from llmwiki.pipeline.run import process_file
from llmwiki.pipeline.watch import Watcher
from test_pipeline_support import UA, make_env, make_eml, responder


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def inbox_files(env, bucket):
    d = env.settings.data_dir / "inbox" / bucket
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()) if d.exists() else []


def test_eml_with_attachment_end_to_end(env):
    eml = make_eml("서버 장애 보고", "2026-09-30 장애 발생, 복구 120분.",
                   [("detail.txt", "# 장애 상세\n\n원인 분석 결과 복구 120분 소요, 영향 사용자 45명.".encode())])
    env.drop("dept-a/part-1", "mail.eml", eml)
    res = env.run_all()
    assert [r.status for r in res] == ["done"] and not res[0].attachment_errors
    assert len(res[0].raw_paths) == 2 and len(res[0].articles) == 2
    assert all(env.store.article_spaces(a) == {"dept-a/part-1"} for a in res[0].articles)
    assert all((env.settings.wiki_dir / a).is_file() for a in res[0].articles)
    assert lint_grounding(env.settings, env.store) == []
    parent, child = res[0].raw_paths
    assert f"attachment of {parent}" in (env.settings.data_dir / child).read_text(encoding="utf-8")
    assert inbox_files(env, "_done") == ["dept-a/part-1/mail.eml"]
    assert {"ingest", "compile"} <= set(env.system_actions())


def test_file_outside_space_is_rejected(env):
    env.drop("", "loose.md", "# 떠돌이\n\n공간 없음 100")
    env.drop("dept-a", "ok.md", "# 정상\n\n매출 200")
    counts = env.watcher.scan_once()
    assert counts["rejected"] == 1 and counts["queued"] == 1
    assert inbox_files(env, "_rejected") == ["loose.md", "loose.md.reason.txt"]
    env.queue.drain(lambda p, s: env.process(p, s))
    assert len(env.store.article_paths()) == 1 and "loose" not in env.store.article_paths()[0]
    assert "ingest_reject" in env.system_actions()


@pytest.mark.parametrize("bad", ["../dept-b", "a/../b", "a\\b", "/abs", "c:x", "a//b", "", "dept-a/", "_done", "."])
def test_invalid_space_rejected(env, bad):
    with pytest.raises(inbox.InvalidSpace):
        inbox.validate_space(bad)
    f = env.drop("dept-a", "t.md", "# t\n\ntext 123")
    res = env.process(f, bad)
    assert res.status == "rejected"
    assert env.store.article_paths() == []
    assert not (env.settings.raw_dir.exists() and any(env.settings.raw_dir.rglob("*.md")))


def test_unsupported_type_rejected_and_moved(env):
    env.drop("dept-a", "sheet.xls", b"\xd0\xcf\x11\xe0")  # legacy binary Excel is not supported (only .xlsx)
    res = env.run_all()
    assert res[0].status == "rejected" and "unsupported" in res[0].error
    assert "dept-a/sheet.xls" in inbox_files(env, "_rejected")


def test_duplicate_drop_skipped_but_other_space_ingested(env):
    env.drop("dept-a", "one.md", "# 보고\n\n금액 500")
    env.run_all()
    env.drop("dept-a", "copy.md", "# 보고\n\n금액 500")
    env.drop("dept-b", "copy.md", "# 보고\n\n금액 500")
    res = env.run_all()
    assert sorted((r.space, r.status) for r in res) == [("dept-a", "skipped"), ("dept-b", "done")]
    assert len(env.store.article_paths()) == 2 and "ingest_skip" in env.system_actions()
    assert inbox_files(env, "_done") == ["dept-a/copy.md", "dept-a/one.md", "dept-b/copy.md"]


def test_failed_compile_does_not_undo_already_registered_raw(env):
    import pytest

    from llmwiki.engine.compile import Compiler
    from llmwiki.engine.llm import FakeLLM, LLMError
    from llmwiki.ingest.text import parse_text
    from llmwiki.pipeline import run as pr

    env.drop("dept-a", "one.md", "# 보고\n\n금액 500")
    env.run_all()
    raws = list((env.settings.raw_dir / "dept-a").glob("*.md"))
    assert len(raws) == 1
    parsed = parse_text("# 보고\n\n금액 500".encode("utf-8"), "one.md")
    def _down(system, prompt):  # an unreachable endpoint (invalid JSON no longer fails: it falls back to New)
        raise LLMError("endpoint down")

    broken = Compiler(env.settings, env.store, FakeLLM(_down))
    with pytest.raises(Exception):
        pr._ingest_doc(parsed, "dept-a", env.settings, broken, env.store, None, pr._db(env.settings), env.audit)
    assert raws[0].exists()  # identical content resolved to the registered raw; a failed retry must not delete it
    assert env.store.raw_space(f"raw/dept-a/{raws[0].name}") == "dept-a"


def test_bad_files_do_not_stop_batch(env):
    env.drop("dept-a", "a-good.md", "# 정상1\n\n값 111")
    env.drop("dept-a", "b-bad.eml", b"\x00\x01 not a mail at all")
    env.drop("dept-a", "c-bad.docx", b"not a zip")
    env.drop("dept-a", "d-badjson.md", "# 깨짐\n\nBADJSON 222")  # invalid model JSON now falls back to a New article
    env.drop("dept-a", "f-llmdown.md", "# 다운\n\nLLMDOWN 444")  # an unreachable endpoint still fails the job
    env.drop("dept-a", "e-good.md", "# 정상2\n\n값 333")
    res = env.run_all()
    final = {r.path.name: r.status for r in res}  # last attempt wins
    assert final == {"a-good.md": "done", "b-bad.eml": "failed", "c-bad.docx": "failed",
                     "d-badjson.md": "done", "e-good.md": "done", "f-llmdown.md": "failed"}
    failed = {j["path"].replace("\\", "/").split("/")[-1]: j for j in env.queue.jobs("failed")}
    assert set(failed) == {"b-bad.eml", "c-bad.docx", "f-llmdown.md"}
    assert all(j["attempts"] == env.queue.max_attempts and j["last_error"] for j in failed.values())
    assert "LLMError" in failed["f-llmdown.md"]["last_error"] and "endpoint down" not in failed["f-llmdown.md"]["last_error"]
    assert "ValueError" in failed["b-bad.eml"]["last_error"] and notes.CORRUPT in failed["b-bad.eml"]["last_error"]
    assert len(env.store.article_paths()) == 3  # a-good, e-good and the fallback article for d-badjson
    moved = [f for f in inbox_files(env, "_failed") if not f.endswith(".reason.txt")]
    assert moved == ["dept-a/b-bad.eml", "dept-a/c-bad.docx", "dept-a/f-llmdown.md"]
    assert not list(env.settings.raw_dir.rglob("*다운*")) and len(list(env.settings.raw_dir.rglob("*.md"))) == 3
    assert all(j["updated_at"].endswith("+00:00") for j in env.queue.jobs())


def test_masking_on_stores_only_masked_text(tmp_path):
    env = make_env(tmp_path, mask=True)
    pii = ["010-1234-5678", "kim@example.com", "900101-1234567"]
    env.drop("dept-a", "pii.md", f"# 연락처\n\n전화 {pii[0]} 메일 {pii[1]} 주민 {pii[2]} 금액 700")
    res = env.run_all()
    assert res[0].status == "done"
    for f in env.settings.data_dir.rglob("*"):
        if f.is_file() and f.suffix in (".md", ".txt"):
            assert not any(v in f.read_text(encoding="utf-8") for v in pii), f
    assert any("[PHONE]" in f.read_text(encoding="utf-8") for f in env.settings.raw_dir.rglob("*.md"))
    assert inbox_files(env, "_done") == ["dept-a/pii.md.processed.txt"]  # original not kept
    assert not any(v in p for _, p in env.llm.calls for v in pii)


def test_mask_policy_per_space(tmp_path):
    env = make_env(tmp_path, mask=True)
    a = env.drop("dept-a", "x.md", "# x\n\n전화 010-1234-5678 금액 10")
    env.process(a, "dept-a", mask_policy={"dept-a": False})
    assert "010-1234-5678" in next(env.settings.raw_dir.rglob("*.md")).read_text(encoding="utf-8")
    assert inbox_files(env, "_done") == ["dept-a/x.md"]


def test_pdf_with_pluggable_ocr(env):
    class Pages:
        def extract_pages(self, data):
            return ["", "텍스트 레이어 총액 800"]

    env.drop("dept-a", "scan.pdf", b"%PDF-fake")
    ocr = FakeOcrEngine({0: "스캔 페이지 합계 900"})
    res = env.run_all(ocr=ocr, extractor=Pages())
    assert res[0].status == "done" and ocr.calls == [0]
    assert "합계 900" in (env.settings.data_dir / res[0].raw_paths[0]).read_text(encoding="utf-8")


def test_scanned_pdf_without_ocr_fails_clearly(env):
    class Empty:
        def extract_pages(self, data):
            return [""]

    f = env.drop("dept-a", "scan.pdf", b"%PDF-fake")
    res = env.process(f, "dept-a", extractor=Empty())
    assert res.status == "failed" and "ValueError" in res.error and notes.UNREADABLE in res.error


def test_mixed_spaces_never_merge_and_acl_holds(env):
    env.drop("dept-a", "w.md", "# 주간 보고\n\n공개 매출 100")
    env.drop("dept-b", "w.md", "# 주간 보고\n\n기밀 예산 9,999만원")
    env.run_all()
    by_space = {tuple(env.store.article_spaces(p)): p for p in env.store.article_paths()}
    assert set(by_space) == {("dept-a",), ("dept-b",)}
    a_path, b_path = by_space[("dept-a",)], by_space[("dept-b",)]
    assert a_path != b_path
    assert "9,999" not in (env.settings.wiki_dir / a_path).read_text(encoding="utf-8")
    echo = FakeLLM()  # echoes its prompt, so any leaked context would be visible
    qs = QueryService(env.settings, env.store, echo, env.audit)
    assert qs.query(UA, "기밀 예산 9,999만원").answer == NO_EVIDENCE
    assert all("9,999" not in p for _, p in echo.calls)
    with pytest.raises(AccessDenied):
        qs.read_article(UA, b_path)
    assert "공개 매출" in qs.read_article(UA, a_path)


def test_redrop_after_done_is_requeued(env):
    env.drop("dept-a", "n.md", "# n\n\n값 12")
    env.run_all()
    env.drop("dept-a", "n.md", "# n2\n\n값 34")
    assert env.watcher.scan_once()["queued"] == 1
    assert env.queue.drain(lambda p, s: env.process(p, s))[0].status == "done"


def test_min_age_waits_for_partial_files(env):
    env.drop("dept-a", "new.md", "# n\n\n값 12")
    w = Watcher(env.settings, env.queue, env.audit, min_age=3600)
    assert w.scan_once() == {"queued": 0, "rejected": 0, "waiting": 1}


def test_compile_calls_never_overlap_across_threads(tmp_path):
    live, peak, lock = [0], [0], threading.Lock()

    def slow(system, prompt):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.02)
        with lock:
            live[0] -= 1
        return responder(system, prompt)

    base = make_env(tmp_path, llm=FakeLLM(slow))
    for i in range(3):
        for j in range(2):
            base.drop(f"dept-{i}", f"f{j}.md", f"# 문서 {i}-{j}\n\n값 {i}{j}1")
    base.watcher.scan_once()
    errors = []

    def worker():
        try:  # each thread has its own connections (sqlite), but shares the process-wide compile lock
            store, audit, q = Store(base.settings.db_path), AuditLog(base.settings.db_path), JobQueue(base.settings)
            q.run_pending(lambda p, s: process_file(p, s, base.settings, base.llm, store, audit))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and peak[0] == 1
    assert len(Store(base.settings.db_path).article_paths()) == 6
