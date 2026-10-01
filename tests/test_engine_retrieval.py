"""ACL-first hybrid retrieval: property tests on synthetic data (no network)."""
from __future__ import annotations

import random
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from llmwiki.acl import can_read
from llmwiki.auth import User
from llmwiki.engine.embed import FakeEmbedder
from llmwiki.engine.llm import FakeLLM
from llmwiki.engine.params import RetrievalParams
from llmwiki.engine.query import NO_EVIDENCE, QueryService
from test_engine_support import UA, UAB, UB, make_env, triage

QUESTION = "분기 매출 보고서 승인 절차는 어떻게 되나요"
SECRET_BODY = "분기 매출 보고서 승인 절차는 어떻게 되나요 정답은 비밀 문서 SECRETMARK"
A_DOCS = [
    ("서버 장애 복구", "서버 장애가 발생하면 백업에서 복구한다 장애 대응 절차"),
    ("휴가 신청", "휴가는 전자결재로 신청하고 팀장이 승인한다"),
    ("출장 정산", "출장비는 영수증을 첨부해 정산한다 보고서 제출"),
    ("보안 점검", "분기마다 보안 점검을 수행하고 보고한다"),
]


def build(tmp_path, with_secret: bool, embedder=None):
    env = make_env(tmp_path)
    env.compiler.embedder = embedder
    for i, (t, b) in enumerate(A_DOCS):
        env.ingest("dept-a", f"a{i}.md", b, triage(title=t, body=b))
    if with_secret:
        env.ingest("dept-b", "secret.md", SECRET_BODY, triage(title="비밀 매출", body=SECRET_BODY))
    return env


def snap(hits):
    return [(h.path, h.score, h.rank) for h in hits]


@pytest.mark.parametrize("mode", ["bm25", "dense", "hybrid"])
@pytest.mark.parametrize("top_k,pool,min_cos,w", [
    (5, 50, 0.0, (1, 1)), (1, 1, 0.0, (1, 1)), (50, 100, -1.0, (0.3, 2)), (3, 2, 0.1, (0, 1))])
def test_unreadable_perfect_match_never_changes_results(tmp_path, mode, top_k, pool, min_cos, w):
    emb = FakeEmbedder()
    params = RetrievalParams(top_k=top_k, candidate_pool=pool, min_cosine=min_cos,
                             w_bm25=w[0], w_dense=w[1], retrieval_mode=mode)
    with_s = build(tmp_path / "w", True, emb)
    without = build(tmp_path / "wo", False, emb)
    q1 = QueryService(with_s.settings, with_s.store, FakeLLM(), with_s.audit, embedder=emb)
    q2 = QueryService(without.settings, without.store, FakeLLM(), without.audit, embedder=emb)
    h1, h2 = q1.retrieve(UA, SECRET_BODY, params), q2.retrieve(UA, SECRET_BODY, params)
    assert snap(h1) == snap(h2)  # identical paths, order AND scores
    secret = set(with_s.store.articles_with_spaces(frozenset({"dept-b"})))
    assert secret and not secret & {h.path for h in h1}
    if mode != "bm25" or pool >= 1:  # the owner of the secret does see it, so the match really was perfect
        assert secret & {h.path for h in q1.retrieve(UB, SECRET_BODY, params)}


def test_prompt_and_citations_exclude_unreadable(tmp_path):
    emb = FakeEmbedder()
    env = build(tmp_path, True, emb)
    llm = FakeLLM()  # echoes the prompt
    res = QueryService(env.settings, env.store, llm, env.audit, embedder=emb).query(UA, QUESTION)
    assert "SECRETMARK" not in llm.calls[0][1] and "SECRETMARK" not in res.answer
    secret_paths = env.store.articles_with_spaces(frozenset({"dept-b"}))
    assert secret_paths and not set(secret_paths) & set(res.citations)
    llm_b = FakeLLM()
    QueryService(env.settings, env.store, llm_b, env.audit, embedder=emb).query(UB, QUESTION)
    assert "SECRETMARK" in llm_b.calls[0][1]


def test_only_unreadable_matches_gives_no_evidence_without_llm(tmp_path):
    emb = FakeEmbedder()
    env = make_env(tmp_path)
    env.compiler.embedder = emb
    env.ingest("dept-b", "s.md", SECRET_BODY, triage(title="비밀", body=SECRET_BODY))
    llm = FakeLLM()
    res = QueryService(env.settings, env.store, llm, env.audit, embedder=emb).query(UA, SECRET_BODY)
    assert res.answer == NO_EVIDENCE and res.citations == [] and llm.calls == []


def test_unreadable_files_are_never_read_or_tokenized(tmp_path, monkeypatch):
    emb = FakeEmbedder()
    env = build(tmp_path, True, emb)
    secret = env.store.articles_with_spaces(frozenset({"dept-b"}))[0]
    import llmwiki.engine.retrieval as r
    seen = []
    orig = r.read_text
    monkeypatch.setattr(r, "read_text", lambda f: (seen.append(f.name), orig(f))[1])
    QueryService(env.settings, env.store, FakeLLM(), env.audit, embedder=emb).retrieve(UA, QUESTION)
    assert seen and all(not secret.endswith(n) for n in seen)


def test_cache_invalidated_by_new_source_and_relabel(tmp_path):
    emb = FakeEmbedder()
    env = build(tmp_path, False, emb)
    qs = QueryService(env.settings, env.store, FakeLLM(), env.audit, embedder=emb)
    q = "냉장고 청소 당번"
    assert all("냉장고" not in h.path for h in qs.retrieve(UA, q))
    rec, res = env.ingest("dept-a", "new.md", "냉장고 청소 당번 규칙", triage(title="냉장고 청소", body="냉장고 청소 당번 규칙"))
    assert res.article in [h.path for h in qs.retrieve(UA, q)]  # new article visible immediately
    env.store.relabel_raw(rec.raw_path, "dept-b")  # admin relabel: dept-a loses it at once
    assert res.article not in [h.path for h in qs.retrieve(UA, q)]
    assert res.article in [h.path for h in qs.retrieve(UB, q)]
    other = sqlite3.connect(str(env.settings.db_path))  # relabel from ANOTHER connection (another process)
    other.execute("UPDATE raw_files SET space = 'dept-a' WHERE raw_path = ?", (rec.raw_path,))
    other.commit()
    other.close()
    assert res.article in [h.path for h in qs.retrieve(UA, q)]
    assert res.article not in [h.path for h in qs.retrieve(UB, q)]


def test_mixed_space_article_needs_all_spaces(tmp_path):
    env = build(tmp_path, False)
    _, res = env.ingest("dept-a", "m.md", "공유 문서 본문 가나다", triage(title="공유", body="공유 문서 본문 가나다"))
    env.ingest("dept-b", "m2.md", "다른 부서", triage(title="다른", body="다른 부서"))
    env.store._db.execute("INSERT INTO article_sources VALUES (?, ?)", (res.article, "raw/dept-b/m2.md"))
    env.store._db.commit()
    env.store._bump()
    qs = QueryService(env.settings, env.store, FakeLLM(), env.audit)
    assert res.article not in [h.path for h in qs.retrieve(UA, "공유 문서")]
    assert res.article in [h.path for h in qs.retrieve(UAB, "공유 문서")]


def test_fast_acl_equals_slow_acl_randomized(tmp_path):
    env = make_env(tmp_path)
    st, rnd = env.store, random.Random(1234)
    pool = ["a", "b", "c", "d/x", "d"]
    for i in range(12):
        st._db.execute("INSERT INTO raw_files VALUES (?, ?, ?)", (f"raw/r{i}.md", rnd.choice(pool + [""]), "h"))
    for i in range(60):
        p = f"t/a{i}.md"
        st._db.execute("INSERT INTO articles VALUES (?, ?, ?)", (p, "t", "2026-01-01"))
        for _ in range(rnd.choice([0, 1, 1, 2, 3, 4])):
            src = f"raw/r{rnd.randrange(14)}.md"  # r12/r13 are unregistered (dangling)
            st._db.execute("INSERT OR IGNORE INTO article_sources VALUES (?, ?)", (p, src))
    st._db.commit()
    for _ in range(200):
        spaces = frozenset(rnd.sample(pool, rnd.randrange(len(pool) + 1)))
        user = User("u", "u", "d", None, spaces)
        slow = {p for p in st.article_paths() if can_read(user, st.article_spaces(p))}
        assert set(st.readable_articles(spaces)) == slow


def test_embedder_failure_at_query_time_falls_back_to_bm25(tmp_path):
    env = build(tmp_path, False, FakeEmbedder())

    class Down:
        model = "fake-embed"

        def embed(self, texts):
            raise RuntimeError("endpoint down")

    base = QueryService(env.settings, env.store, FakeLLM(), env.audit).retrieve(UA, QUESTION)
    down = QueryService(env.settings, env.store, FakeLLM(), env.audit, embedder=Down()).retrieve(UA, QUESTION)
    assert base and [h.path for h in down] == [h.path for h in base]


def test_compile_succeeds_when_embedder_raises_and_article_is_unembedded(tmp_path, caplog):
    class Down:
        model = "fake-embed"

        def embed(self, texts):
            raise RuntimeError("endpoint down: " + texts[0])

    env = make_env(tmp_path)
    env.compiler.embedder = Down()
    with caplog.at_level("WARNING"):
        _, res = env.ingest("dept-a", "x.md", "보안 점검 내용 UNIQUETEXT", triage(title="보안", body="보안 점검 내용 UNIQUETEXT"))
    assert res.article and env.store.embedding_meta() == {}
    assert "UNIQUETEXT" not in caplog.text and "embedding failed" in caplog.text
    hits = QueryService(env.settings, env.store, FakeLLM(), env.audit, embedder=FakeEmbedder()).retrieve(UA, "보안 점검")
    assert [h.path for h in hits] == [res.article]  # still found via BM25


def test_compile_embeds_update_replaces_vector_and_delete_removes(tmp_path):
    emb = FakeEmbedder()
    env = make_env(tmp_path)
    env.compiler.embedder = emb
    _, res = env.ingest("dept-a", "x.md", "첫 번째 내용입니다", triage(title="문서", body="첫 번째 내용입니다"))
    m1 = env.store.embedding_meta()[res.article]
    assert m1[0] == "fake-embed" and m1[1] == emb.dim
    new_body = "첫 번째 내용입니다 두 번째 추가 내용입니다 더 길게 씁니다"
    env.ingest("dept-a", "y.md", "두 번째 추가 내용입니다", triage("Update", target=res.article, title="문서", body=new_body))
    assert env.store.embedding_meta()[res.article][2] != m1[2]
    env.store.delete_article(res.article)
    assert env.store.embedding_meta() == {} and env.store.readable_articles(UA.spaces) == {}


def test_params_defaults_temperature_and_context_cap(tmp_path):
    env = build(tmp_path, False)
    llm = FakeLLM()
    qs = QueryService(env.settings, env.store, llm, env.audit)
    qs.query(UA, "서버 장애 복구")
    assert llm.temperatures == [None]
    qs.query(UA, "서버 장애 복구", params=RetrievalParams(top_k=1, temperature=0.7, max_context_chars=5))
    assert llm.temperatures[-1] == 0.7 and "백업에서" not in llm.calls[-1][1]
    with pytest.raises(ValueError):
        RetrievalParams(retrieval_mode="nope")


def test_retrieve_writes_no_audit_and_ranks_are_one_based(tmp_path):
    env = build(tmp_path, False)
    qs = QueryService(env.settings, env.store, FakeLLM(), env.audit)
    n = len(env.audit.entries(None))
    hits = qs.retrieve(UA, "보고서", RetrievalParams(top_k=2))
    assert 0 < len(hits) <= 2 and [h.rank for h in hits] == list(range(1, len(hits) + 1))
    assert len(env.audit.entries(None)) == n


def test_cross_thread_queries_match_serial_while_store_is_written(tmp_path):
    emb = FakeEmbedder()
    env = build(tmp_path, True, emb)
    qs = QueryService(env.settings, env.store, FakeLLM(), env.audit, embedder=emb)
    users = [UA, UB, UAB]
    serial = {u.id: snap(qs.retrieve(u, QUESTION)) for u in users}
    errors: list = []

    def work(i):
        try:
            u = users[i % 3]
            assert snap(qs.retrieve(u, QUESTION)) == serial[u.id]
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def writer():
        for j in range(20):
            env.store.set_embedding("nonexistent.md", "m", 1, str(j), b"\0\0\0\0")  # bumps the version

    t = threading.Thread(target=writer)
    t.start()
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(work, range(60)))
    t.join()
    assert not errors
