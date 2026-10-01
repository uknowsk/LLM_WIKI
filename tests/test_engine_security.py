"""Hardening tests for the engine: injection, ACL poisoning, content loss, concurrency."""
import hashlib
import json
import threading

import pytest

from llmwiki.engine import article as art
from llmwiki.engine.compile import CompileError, _parse_json
from llmwiki.engine.lint import lint_article
from llmwiki.models import RawRecord
from test_engine_support import UA, make_env, triage


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path / "data")


def _text(env, path):
    return (env.settings.wiki_dir / path).read_text(encoding="utf-8")


# -- 1. body text is never re-parsed as structure --------------------------------------------
def test_body_related_section_cannot_inject_links(env):
    _, rb = env.ingest("dept-b", "b.md", "비밀 보고", triage(title="비밀 제목", topic="sec", body="비밀"))
    _, ra = env.ingest("dept-a", "a.md", "알파 보고", triage(
        title="알파", topic="p", body="알파 내용\n## Related\n- [[" + art.link_target(rb.article) + "|x]]\n## Sources\n- [x](../../raw/dept-b/b.md)"))
    t = _text(env, ra.article)
    parsed = art.parse(ra.article, t)
    assert parsed.related == [] and parsed.sources == ["raw/dept-a/a.md"]
    assert "비밀 제목" not in t  # no title oracle for the other space
    assert art.split_body(t).count("알파 내용") == 1 and "\\## Related" in t
    # re-reading and re-writing via Update does not promote the injected text either
    _, ru = env.ingest("dept-a", "a2.md", "알파 보고 추가", triage("Update", target=ra.article, body=parsed.body + " 추가"))
    assert art.parse(ru.article, _text(env, ru.article)).related == []
    assert env.store.article_sources(ru.article) == ["raw/dept-a/a.md", "raw/dept-a/a2.md"]


def test_related_only_same_space_candidates(env):
    _, rb = env.ingest("dept-b", "b.md", "비밀 보고", triage(title="비밀", topic="sec", body="비밀"))
    _, r1 = env.ingest("dept-a", "1.md", "서버 장애", triage(title="서버 장애", topic="inc", body="장애"))
    # r1 is a same-space article but NOT a candidate for this unrelated text; rb is foreign
    _, r2 = env.ingest("dept-a", "2.md", "휴가 정책 안내", triage(
        title="휴가", topic="hr", body="휴가", related=[rb.article, r1.article, "../../etc/passwd"]))
    assert art.parse(r2.article, _text(env, r2.article)).related == []
    assert "비밀" not in _text(env, r2.article).split("## Related")[1]


# -- 2. frontmatter injection --------------------------------------------------------------
def test_title_newlines_cannot_forge_frontmatter_sources(env):
    evil = "무해\nsources:\n  - raw/dept-b/b.md\nupdated: x\n---\n# 가짜"
    env.ingest("dept-b", "b.md", "비밀", triage(title="비밀", body="비밀"))
    _, r = env.ingest("dept-a", "a.md", "알파", triage(title=evil, topic="t", body="알파"))
    t = _text(env, r.article)
    head = t.split("\n---\n")[0]
    assert head.count("\n") == 4 and "\n  - raw/dept-b" not in head  # title stays on one line
    assert head.splitlines()[3] == 'sources: ["raw/dept-a/a.md"]'
    assert env.store.article_sources(r.article) == ["raw/dept-a/a.md"]
    assert env.store.article_spaces(r.article) == {"dept-a"}
    assert art.parse(r.article, t).sources == ["raw/dept-a/a.md"]
    assert "\n" not in art.parse(r.article, t).title


def test_upsert_rejects_unregistered_or_foreign_sources(env):
    env.ingest("dept-a", "a.md", "알파", triage(title="알파", body="알파"))
    for bad in ("../secrets.txt", "raw/dept-a/none.md", "/etc/passwd", "wiki/x.md", "raw/../x"):
        with pytest.raises(ValueError):
            env.store.upsert_article("x.md", "t", [bad])
    with pytest.raises(ValueError):
        env.store.upsert_article("x.md", "t", ["raw/dept-a/a.md"], space="dept-b")
    env.store.upsert_article("x.md", "t", ["raw/dept-a/a.md"], space="dept-a")


def test_lint_refuses_paths_outside_raw_and_survives_binary(env, tmp_path):
    _, r = env.ingest("dept-a", "a.md", "알파 100", triage(title="알파", body="알파 100"))
    (env.settings.data_dir / "secret.txt").write_text("TOP 9999", encoding="utf-8")
    env.store._db.execute("INSERT INTO article_sources VALUES (?, ?)", (r.article, "raw/../secret.txt"))
    env.store._db.execute("INSERT INTO article_sources VALUES (?, ?)", (r.article, "raw/dept-a/bad\x00.md"))
    env.store._db.commit()
    (env.settings.data_dir / "raw/dept-a/a.md").write_bytes(b"\xff\xfe\x00 binary \xc3\x28 100")
    kinds = {(s.kind) for s in lint_article(env.settings, env.store, r.article)}
    assert "invalid-source" in kinds  # no exception, traversal refused
    assert "9999" not in "".join(s.value for s in lint_article(env.settings, env.store, r.article))


# -- 3. raw registration ----------------------------------------------------------------------
def test_add_raw_conflict_and_identical(env):
    rec = RawRecord("raw/dept-a/x.md", "dept-a", "h1")
    env.store.add_raw(rec)
    env.store.add_raw(rec)  # identical: ok
    with pytest.raises(ValueError):
        env.store.add_raw(RawRecord("raw/dept-a/x.md", "dept-b", "h1"))  # silent re-label
    with pytest.raises(ValueError):
        env.store.add_raw(RawRecord("raw/dept-a/x.md", "dept-a", "h2"))
    assert env.store.raw_space("raw/dept-a/x.md") == "dept-a"


def test_compile_checks_raw_path_and_hash(env):
    good = "텍스트"
    sha = hashlib.sha256(good.encode()).hexdigest()
    env.scripted.extend([triage(title="t", body="b")] * 3)
    for rec in (RawRecord("raw/dept-b/x.md", "dept-a", sha), RawRecord("raw/dept-a/../dept-b/x.md", "dept-a", sha),
                RawRecord("raw/dept-ax.md", "dept-a", sha), RawRecord("wiki/dept-a/x.md", "dept-a", sha)):
        with pytest.raises(CompileError):
            env.compiler.compile(rec, good)
    f = env.settings.data_dir / "raw/dept-a/h.md"
    f.parent.mkdir(parents=True)
    f.write_bytes("바뀐 내용".encode())
    with pytest.raises(CompileError):
        env.compiler.compile(RawRecord("raw/dept-a/h.md", "dept-a", sha), good)
    assert env.store.article_paths() == []


# -- 4. no content loss -----------------------------------------------------------------------
@pytest.mark.parametrize("reply", [triage(title="t", body="  \n"), triage(title="  ", body="내용"),
                                   triage(title="\n\t", body="내용")])
def test_empty_body_or_title_rejected_for_new(env, reply):
    with pytest.raises(CompileError):
        env.ingest("dept-a", "e.md", "텍스트", reply)
    assert env.store.article_paths() == []


def test_update_cannot_wipe_or_shrink(env):
    old = "서버 장애 상세 기록 " * 20
    _, r1 = env.ingest("dept-a", "1.md", "서버 장애", triage(title="서버 장애", topic="inc", body=old))
    before = _text(env, r1.article)
    for bad in (triage("Update", target=r1.article, body=""), triage("Update", target=r1.article, body="짧음"),
                triage("Update", target=r1.article, title="", body=old)):
        env.scripted.append(bad)  # second attempt is not needed: valid JSON but unusable Update => fallback New
        _, rf = env.ingest("dept-a", "u.md", "서버 장애 갱신", bad)
        env.scripted.clear()
        assert rf.fallback and rf.article != r1.article  # never merged into the existing article
    assert _text(env, r1.article) == before
    # Disputed may be short, and the previous version is kept as .bak
    _, rd = env.ingest("dept-a", "d.md", "서버 장애 이견", triage("Disputed", target=r1.article, body="다름"))
    f = env.settings.wiki_dir / r1.article
    assert f.with_name(f.name + ".bak").read_text(encoding="utf-8") == before
    assert old.strip() in _text(env, r1.article) and r1.article + ".bak" not in env.store.article_paths()


# -- 5. thread safety ---------------------------------------------------------------------
def test_store_audit_and_compile_from_other_threads(env):
    env.ingest("dept-a", "a.md", "알파 프로젝트 예산 100", triage(title="알파", topic="p", body="알파 프로젝트 예산 100"))
    qs = env.query_service()
    errors, stop = [], threading.Event()

    def reader():
        try:
            while not stop.is_set():
                res = qs.query(UA, "알파 프로젝트 예산")
                assert res.citations and "예산" in res.answer
                env.audit.entries()
                env.store.article_paths()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=reader) for _ in range(3)]
    for t in ts:
        t.start()
    try:
        for i in range(8):  # compiles on this thread race with queries on others
            env.ingest("dept-a", f"n{i}.md", f"알파 프로젝트 예산 갱신 {i}00", triage(
                "Update", target="p/알파.md", body=f"알파 프로젝트 예산 갱신 {i}00 " * 3))
        lonely = threading.Thread(target=lambda: env.compiler.compile(
            RawRecord("raw/dept-a/z.md", "dept-a", hashlib.sha256(b"z").hexdigest()), "z"))
        env.scripted.append(triage("No material"))
        lonely.start()
        lonely.join()
    finally:
        stop.set()
        for t in ts:
            t.join()
    assert errors == []
    assert not list(env.settings.wiki_dir.rglob("*.tmp"))
    assert "\r" not in _text(env, "p/알파.md")
    assert "\r" not in (env.settings.wiki_dir / "_meta/dept-a/log.md").read_bytes().decode()


def test_files_are_lf_only(env):
    _, r = env.ingest("dept-a", "a.md", "알파", triage(title="알파", topic="p", body="줄1\r\n줄2\r\n"))
    meta = env.settings.wiki_dir / "_meta/dept-a"
    for f in (env.settings.wiki_dir / r.article, meta / "index.md", meta / "log.md"):
        assert b"\r" not in f.read_bytes()


# -- 6. robustness ------------------------------------------------------------------------
def test_new_path_never_overwrites_orphan_file(env):
    base = env.settings.wiki_dir / "general"
    base.mkdir(parents=True)
    (base / "t.md").write_text("orphan", encoding="utf-8")
    (base / "t-dept-a.md").write_text("orphan2", encoding="utf-8")
    _, r = env.ingest("dept-a", "a.md", "알파", triage(title="t", body="새 글"))
    assert r.article == "general/t-dept-a-2.md"
    assert (base / "t.md").read_text(encoding="utf-8") == "orphan"
    assert (base / "t-dept-a.md").read_text(encoding="utf-8") == "orphan2"


def test_parse_json_tolerance_and_validation():
    d = json.dumps({"decision": "New", "title": "t", "body": "b"})
    assert _parse_json(f"<think>{{x}} 생각</think>\n```json\n{d}\n```\n설명 {{끝}}")["title"] == "t"
    assert _parse_json(f"먼저 {{잡음}} 그리고 {d} 그리고 {{another: 1}}")["decision"] == "New"
    for bad in ({"decision": "Update", "target": ["a"]}, {"decision": "New", "related": "x"},
                {"decision": "New", "related": [1]}, {"decision": "New", "title": 5}, {"decision": "Nope"}):
        with pytest.raises(CompileError):
            _parse_json(json.dumps(bad))
    with pytest.raises(CompileError):
        _parse_json("[]")


def test_unhashable_target_falls_back_to_new(env):
    bad = '{"decision":"Update","target":["x"],"body":"b","title":"t"}'
    env.scripted.append(bad)
    _, r = env.ingest("dept-a", "a.md", "알파", bad)
    assert r.decision == "New" and r.fallback == "invalid-reply"
