"""Regression tests for the retrieval security review (cache staleness, embedding DoS, tampered vectors)."""
from __future__ import annotations

import hashlib

from llmwiki.engine.embed import FakeEmbedder, normalize, pack
from llmwiki.engine.llm import FakeLLM
from llmwiki.engine.params import RetrievalParams
from llmwiki.engine.query import QueryService
from llmwiki.models import RawRecord
from test_engine_support import UA, make_env, triage


def _put(env, name, text):
    """Register a raw and write+register an article directly (no compile)."""
    raw = f"raw/dept-a/{name}.raw.md"
    env.store.add_raw(RawRecord(raw, "dept-a", hashlib.sha256(raw.encode()).hexdigest()))
    f = env.settings.wiki_dir / f"{name}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"---\ntitle: \"{name}\"\n---\n# {name}\n\n{text}\n", encoding="utf-8")
    env.store.upsert_article(f"{name}.md", name, [raw], space="dept-a")


def _qs(env, emb=None):
    return QueryService(env.settings, env.store, FakeLLM(), env.audit, embedder=emb)


def test_delete_and_recreate_does_not_serve_stale_tokens(tmp_path):
    env = make_env(tmp_path)
    _put(env, "c", "zebra zebra")
    qs = _qs(env)
    assert [h.path for h in qs.retrieve(UA, "zebra")] == ["c.md"]
    env.store.delete_article("c.md")
    (env.settings.wiki_dir / "c.md").unlink()
    _put(env, "c", "quokka")
    assert qs.retrieve(UA, "zebra") == []
    assert [h.path for h in qs.retrieve(UA, "quokka")] == ["c.md"]


def test_vector_cache_follows_model_change(tmp_path):
    env = make_env(tmp_path)
    _put(env, "d", "alpha beta gamma")
    emb = FakeEmbedder(model="m1")
    qs = _qs(env, emb)
    from llmwiki.engine.embed import embed_article
    assert embed_article(env.store, emb, "d.md", (env.settings.wiki_dir / "d.md").read_text(encoding="utf-8")) == "ok"
    p = RetrievalParams(retrieval_mode="dense")
    assert qs.retrieve(UA, "alpha beta gamma", p)
    # same text hash, same dim, other model: stored vector replaced by an orthogonal-ish one
    sha = env.store.embedding_meta()["d.md"][2]
    other = [0.0] * emb.dim
    other[0] = 1.0
    env.store.set_embedding("d.md", "m2", emb.dim, sha, pack(normalize(other)))
    assert qs.retrieve(UA, "alpha beta gamma", p) == []  # m1 embedder must not use the m2 vector
    emb2 = FakeEmbedder(model="m2")
    qs2 = _qs(env, emb2)
    v = qs2._retrieval._vectors(qs2._retrieval.snapshot(UA.spaces), "m2", emb.dim)
    assert list(v["d.md"])[:1] == [1.0]


class _Clock:
    t = 100.0

    def __call__(self):
        return self.t


class _Flaky:
    model = "fake-embed"

    def __init__(self):
        self.inner, self.fail, self.calls = FakeEmbedder(), False, 0

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("stall")
        return self.inner.embed(texts)


def test_failure_cooldown_and_query_embedding_cache(tmp_path):
    env = make_env(tmp_path)
    emb = _Flaky()
    env.compiler.embedder = emb
    env.ingest("dept-a", "x.md", "서버 장애 복구", triage(title="장애", body="서버 장애 복구"))
    qs, clock = _qs(env, emb), _Clock()
    qs._retrieval.clock = clock
    base = emb.calls
    first = qs.retrieve(UA, "서버 장애")
    qs.retrieve(UA, "서버 장애")
    assert emb.calls == base + 1 and first  # second identical question served from the LRU
    emb.fail = True
    qs.retrieve(UA, "다른 질문 장애")
    n = emb.calls
    for _ in range(5):
        assert qs.retrieve(UA, "또 다른 질문 장애")  # BM25 fallback still answers
    assert emb.calls == n  # cooldown: no more endpoint calls
    clock.t += 31
    qs.retrieve(UA, "또 다른 질문 장애")
    assert emb.calls == n + 1  # retried after the cooldown


def test_query_embed_uses_short_timeout(tmp_path, monkeypatch):
    from llmwiki.engine.embed import OpenAICompatEmbedder
    monkeypatch.setenv("WIKI_EMBED_QUERY_TIMEOUT", "2.5")
    e = OpenAICompatEmbedder("http://127.0.0.1:1/v1", "m")
    assert e.query_timeout == 2.5 and e.timeout == 60.0
    monkeypatch.delenv("WIKI_EMBED_QUERY_TIMEOUT")
    assert OpenAICompatEmbedder("http://127.0.0.1:1/v1", "m").query_timeout == 5.0


def test_snapshot_uses_inequality_not_ordering(tmp_path):
    env = make_env(tmp_path)
    _put(env, "e", "hello world")
    qs = _qs(env)
    qs.retrieve(UA, "hello")
    st = qs._retrieval._st
    st.version = (st.version[0] + 5, st.version[1] + 5)  # pretend the cache saw a "later" tuple
    st.snaps[frozenset(UA.spaces)] = type(st.snaps[frozenset(UA.spaces)])({}, {})  # poisoned entry
    assert [h.path for h in qs.retrieve(UA, "hello")] == ["e.md"]
    assert st.version == env.store.version and frozenset(UA.spaces) in st.snaps  # any change resets and re-caches


def test_tampered_vectors_are_ignored(tmp_path):
    env = make_env(tmp_path)
    emb = FakeEmbedder()
    env.compiler.embedder = emb
    for i, t in enumerate(("가나다 라마바", "사아자 차카타", "파하 거너더")):
        env.ingest("dept-a", f"t{i}.md", t, triage(title=f"문서{i}", body=t))
    paths = env.store.article_paths()
    meta = env.store.embedding_meta()
    nan = [float("nan")] + [0.1] * (emb.dim - 1)
    big = [5.0] * emb.dim  # norm != 1
    from array import array
    env.store.set_embedding(paths[0], emb.model, emb.dim, meta[paths[0]][2], array("f", nan).tobytes())
    env.store.set_embedding(paths[1], emb.model, emb.dim, meta[paths[1]][2], array("f", big).tobytes())
    qs = _qs(env, emb)
    r = qs._retrieval
    vecs = r._vectors(r.snapshot(UA.spaces), emb.model, emb.dim)
    assert set(vecs) == {paths[2]}
    hits = qs.retrieve(UA, "가나다 라마바", RetrievalParams(retrieval_mode="hybrid"))
    assert paths[0] in [h.path for h in hits]  # tampered article is still found via BM25
