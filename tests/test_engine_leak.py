"""Cross-space leak tests: a dept-a user must never obtain dept-b content."""
import pytest

from llmwiki.engine.llm import FakeLLM
from llmwiki.engine.query import AccessDenied, NO_EVIDENCE
from llmwiki.models import RawRecord
from test_engine_support import UA, UAB, UB, make_env, triage

SECRET = "9,999만원"


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path / "data")
    e.ingest("dept-a", "a.md", "알파 프로젝트 일정은 2026-10-05 이고 예산 1,200만원 이다.",
             triage(title="알파 프로젝트", topic="project", body="알파 프로젝트 예산 1,200만원 일정 2026-10-05"))
    e.ingest("dept-b", "b.md", f"베타 프로젝트 기밀 예산 {SECRET} 이다.",
             triage(title="베타 프로젝트", topic="project", body=f"베타 프로젝트 기밀 예산 {SECRET}"))
    return e


def actions(e, uid):
    return [(a, t, d) for _, u, a, t, d in e.audit.entries() if u == uid]


def test_a_query_answer_has_no_other_space_content(env):
    llm = FakeLLM()  # echoes the prompt: any leaked context would show in the answer
    res = env.query_service(llm).query(UA, "베타 프로젝트 예산")
    assert SECRET not in res.answer
    assert all(SECRET not in p for _, p in llm.calls)
    # a question that matches both spaces only surfaces dept-a
    res = env.query_service(llm).query(UA, "프로젝트 예산")
    assert SECRET not in res.answer and "1,200만원" in res.answer


def test_b_citation_list_only_readable(env):
    res = env.query_service().query(UA, "프로젝트 예산")
    assert res.citations and all(env.store.article_spaces(c) == {"dept-a"} for c in res.citations)
    res = env.query_service().query(UA, "베타 기밀")
    assert res.answer == NO_EVIDENCE and res.citations == []


def test_c_mixed_source_article_hidden_from_single_space_user(env):
    # merged article built from a dept-a raw and a dept-b raw
    env.store.upsert_article("mixed/merge.md", "혼합", ["raw/dept-a/a.md", "raw/dept-b/b.md"])
    f = env.settings.wiki_dir / "mixed" / "merge.md"
    f.parent.mkdir(parents=True)
    f.write_text(f"---\ntitle: 혼합\n---\n# 혼합\n혼합 문서 {SECRET} 알파 베타\n", encoding="utf-8")
    llm = FakeLLM()
    qs = env.query_service(llm)
    for user in (UA, UB):
        res = qs.query(user, "혼합 문서 알파 베타")
        assert "mixed/merge.md" not in res.citations
        assert "MIXEDONLY7731" not in res.answer  # mixed article text never reaches the answer ...
        if user is UA:
            assert SECRET not in res.answer
        with pytest.raises(AccessDenied):
            qs.read_article(user, "mixed/merge.md")
    assert all("MIXEDONLY7731" not in p for _, p in llm.calls)  # ... nor the LLM prompt
    assert "mixed/merge.md" in qs.query(UAB, "혼합 문서").citations  # holder of both spaces may read
    assert qs.read_article(UAB, "mixed/merge.md")


def test_c2_compile_never_offers_other_space_or_mixed_articles(env):
    env.store.upsert_article("mixed/merge.md", "혼합", ["raw/dept-a/a.md", "raw/dept-b/b.md"])
    (env.settings.wiki_dir / "mixed").mkdir()
    (env.settings.wiki_dir / "mixed" / "merge.md").write_text(f"---\ntitle: x\n---\n# x\n베타 {SECRET} 알파\n", encoding="utf-8")
    env.ingest("dept-a", "a2.md", "알파 프로젝트 베타 추가 내용", triage("No material"))
    prompt = env.compile_llm.calls[-1][1]
    assert SECRET not in prompt


def test_d_read_api(env):
    qs = env.query_service()
    b_article = env.store.articles_with_spaces(frozenset({"dept-b"}))[0]
    a_article = env.store.articles_with_spaces(frozenset({"dept-a"}))[0]
    assert qs.read_article(UA, a_article)
    for bad in (b_article, "../raw/dept-b/b.md", "nope.md", "_meta/dept-b/index.md"):
        with pytest.raises(AccessDenied):
            qs.read_article(UA, bad)
    assert ("read_denied", b_article, "") in actions(env, "ua")
    assert ("read", a_article, "") in actions(env, "ua")


def test_unlabeled_denied(env):
    # article whose source raw has no label row, and one without any source
    env.store._db.execute("INSERT INTO articles VALUES ('x/orphan.md', '고아', '2026-01-01')")  # bypass validation
    env.store._db.execute("INSERT INTO article_sources VALUES ('x/orphan.md', 'raw/unknown/zzz.md')")
    env.store._db.commit()
    env.store.upsert_article("x/nosrc.md", "무출처", [])
    for p in ("x/orphan.md", "x/nosrc.md"):
        (env.settings.wiki_dir / "x").mkdir(exist_ok=True)
        (env.settings.wiki_dir / p).write_text("---\ntitle: t\n---\n# t\n고아 무출처\n", encoding="utf-8")
        with pytest.raises(AccessDenied):
            env.query_service().read_article(UAB, p)
    assert env.query_service().query(UAB, "고아 무출처").answer == NO_EVIDENCE
    with pytest.raises(ValueError):
        env.store.add_raw(RawRecord("raw/x.md", "", "h"))


def test_audit_rows_written(env):
    qs = env.query_service()
    qs.query(UA, "프로젝트 예산")
    qs.query(UB, "존재하지않는질문xyz")
    rows = env.audit.entries()
    assert [(u, a) for _, u, a, _, _ in rows if a == "query"] == [("ua", "query"), ("ub", "query")]
    assert any(d == "no-evidence" for *_, d in rows) and any(d.startswith("cited: ") for *_, d in rows)
