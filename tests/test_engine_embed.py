"""Embedder client (SSRF rules, no network beyond a loopback stub), reindex CLI logic, cosine benchmark."""
from __future__ import annotations

import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from llmwiki.config import Settings
from llmwiki.engine.embed import (EmbedError, FakeEmbedder, OpenAICompatEmbedder, embedder_from_env, normalize, pack)
from llmwiki.engine.llm import FakeLLM
from llmwiki.engine.params import RetrievalParams
from llmwiki.engine.query import QueryService
from llmwiki.engine.reindex import reindex
from test_engine_support import UA, make_env, triage

S = Settings("development", None, "dev", "http://127.0.0.1:1234/v1", "m", False)  # type: ignore[arg-type]


class _Stub(BaseHTTPRequestHandler):
    mode = "ok"
    seen: list = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        _Stub.seen.append((self.path, body))
        if _Stub.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.end_headers()
            return
        if _Stub.mode == "bad":
            data = {"data": [{"index": 0, "embedding": [1.0]}]}
        else:  # reply in reverse order to prove `index` is honored
            data = {"data": [{"index": i, "embedding": [float(i + 1), 0.0, 1.0]} for i in reversed(range(len(body["input"])))]}
        out = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture()
def stub():
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    _Stub.mode, _Stub.seen = "ok", []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


@pytest.mark.parametrize("url", ["http://8.8.8.8/v1", "http://0x08080808/v1", "ftp://127.0.0.1/v1", "http:///v1",
                                 "http://[::ffff:8.8.8.8]/v1"])
def test_non_intranet_or_odd_endpoints_are_rejected(url):
    with pytest.raises(ValueError):
        OpenAICompatEmbedder(url, "m")


def test_batching_order_and_request_shape(stub):
    emb = OpenAICompatEmbedder(stub, "text-embedding-bge-m3", batch_size=2)
    out = emb.embed(["a", "b", "c"])
    assert [v[0] for v in out] == [1.0, 2.0, 1.0]  # sorted back by index within each batch of 2 + 1
    assert [p for p, _ in _Stub.seen] == ["/v1/embeddings"] * 2
    assert _Stub.seen[0][1] == {"model": "text-embedding-bge-m3", "input": ["a", "b"]}


def test_redirect_is_refused_and_bad_reply_is_embed_error(stub):
    emb = OpenAICompatEmbedder(stub, "m")
    _Stub.mode = "redirect"
    with pytest.raises(EmbedError) as e:
        emb.embed(["SECRETTEXT"])
    assert "SECRETTEXT" not in str(e.value) and len(_Stub.seen) == 1  # the redirect target was never contacted
    _Stub.mode = "bad"
    with pytest.raises(EmbedError):
        emb.embed(["x", "y"])


def test_unreachable_endpoint_is_embed_error():
    with pytest.raises(EmbedError):
        OpenAICompatEmbedder("http://127.0.0.1:9/v1", "m", timeout=2).embed(["x"])


def test_embedder_from_env_defaults_and_off():
    assert embedder_from_env(S, {}).model == "text-embedding-bge-m3"
    assert embedder_from_env(S, {"WIKI_EMBED_MODEL": "my-model"}).model == "my-model"
    assert embedder_from_env(S, {"WIKI_EMBED_MODEL": "off"}) is None
    assert embedder_from_env(S, {"WIKI_EMBED_MODEL": ""}) is None
    assert embedder_from_env(S, {}).base_url == "http://127.0.0.1:1234/v1"


def test_fake_embedder_is_deterministic_and_normalized():
    e = FakeEmbedder()
    a, b = e.embed(["서버 장애"]), e.embed(["서버 장애"])
    assert a == b and abs(sum(x * x for x in a[0]) - 1) < 1e-9


def test_reindex_is_idempotent_and_resumable(tmp_path):
    env = make_env(tmp_path)
    for i in range(3):
        env.ingest("dept-a", f"d{i}.md", f"문서 {i} 내용", triage(title=f"문서{i}", body=f"문서 {i} 내용"))
    emb = FakeEmbedder()
    assert env.store.embedding_meta() == {}
    assert reindex(env.settings, env.store, emb) == {"embedded": 3, "skipped": 0, "failed": 0, "missing_file": 0}
    assert reindex(env.settings, env.store, emb) == {"embedded": 0, "skipped": 3, "failed": 0, "missing_file": 0}
    victim = env.store.article_paths()[0]
    env.store.delete_embedding(victim)  # simulate an interrupted earlier run
    assert reindex(env.settings, env.store, emb)["embedded"] == 1
    assert emb.calls == 4
    # changed model => everything is re-embedded
    assert reindex(env.settings, env.store, FakeEmbedder(model="other"))["embedded"] == 3


def test_reindex_stops_when_endpoint_down(tmp_path):
    env = make_env(tmp_path)
    for i in range(3):
        env.ingest("dept-a", f"d{i}.md", f"문서 {i} 내용", triage(title=f"문서{i}", body=f"문서 {i} 내용"))

    class Down:
        model = "x"

        def embed(self, texts):
            raise EmbedError("down")

    counts = reindex(env.settings, env.store, Down())
    assert counts["failed"] == 1 and counts["embedded"] == 0


def test_query_with_embedder_but_unembedded_articles_still_works(tmp_path):
    env = make_env(tmp_path)  # compiled WITHOUT an embedder: no vectors at all
    env.ingest("dept-a", "d.md", "서버 장애 복구 절차", triage(title="장애", body="서버 장애 복구 절차"))
    hits = QueryService(env.settings, env.store, FakeLLM(), env.audit, embedder=FakeEmbedder()).retrieve(UA, "장애 복구")
    assert len(hits) == 1


def test_mode_dense_and_hybrid_degrade_without_vectors(tmp_path):
    env = make_env(tmp_path)
    env.ingest("dept-a", "d.md", "서버 장애 복구 절차", triage(title="장애", body="서버 장애 복구 절차"))
    qs = QueryService(env.settings, env.store, FakeLLM(), env.audit)
    assert qs.retrieve(UA, "장애", RetrievalParams(retrieval_mode="dense")) == []
    assert len(qs.retrieve(UA, "장애", RetrievalParams(retrieval_mode="hybrid"))) == 1


def test_cosine_benchmark_2000x1024(tmp_path, capsys):
    env = make_env(tmp_path)
    st, rnd, dim, n = env.store, random.Random(7), 1024, 2000
    st.add_raw(__import__("llmwiki.models", fromlist=["RawRecord"]).RawRecord("raw/dept-a/b.md", "dept-a", "h"))
    wiki = env.settings.wiki_dir / "bench"
    wiki.mkdir(parents=True)
    for i in range(n):
        p = f"bench/a{i}.md"
        st.upsert_article(p, f"t{i}", ["raw/dept-a/b.md"], space="dept-a")
        (wiki / f"a{i}.md").write_text(f"문서 {i} 서버 장애 복구 절차 항목{i % 50}", encoding="utf-8")
        v = normalize([rnd.gauss(0, 1) for _ in range(dim)])
        st.set_embedding(p, "fake-embed", dim, "h", pack(v))
    emb = FakeEmbedder(dim=dim)
    qs = QueryService(env.settings, st, FakeLLM(), env.audit, embedder=emb)
    timings = {}
    for mode in ("dense", "hybrid"):
        t0 = time.perf_counter()
        cold = qs.retrieve(UA, "서버 장애 복구", RetrievalParams(retrieval_mode=mode))
        timings[mode + "_cold_ms"] = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        for _ in range(5):
            warm = qs.retrieve(UA, "서버 장애 복구", RetrievalParams(retrieval_mode=mode))
        timings[mode + "_warm_ms"] = (time.perf_counter() - t0) * 200
        assert warm == cold and len(warm) == RetrievalParams().top_k
    with capsys.disabled():
        print("\nBENCH 2000x1024:", {k: round(v, 1) for k, v in timings.items()})
    assert timings["hybrid_warm_ms"] < 5000  # generous: this is a report, not a gate
