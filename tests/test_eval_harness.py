import json
import sqlite3

import pytest

from llmwiki.engine.embed import FakeEmbedder
from llmwiki.engine.llm import FakeLLM, LLMError
from llmwiki.engine.params import RetrievalParams
from llmwiki.eval.build import build
from llmwiki.eval.dataset import GoldMap, doc_id, load_qa
from llmwiki.eval.final import HeldoutGuardError, params_hash, run_final
from llmwiki.eval.llmcache import CachedLLM
from llmwiki.eval.runner import settings_for
from llmwiki.eval.scoring import summarize
from test_eval_support import echo_llm, make_harness, make_snapshot, write_corpus
from test_pipeline_support import responder

BM25 = RetrievalParams(retrieval_mode="bm25", top_k=3)


def rows_by_id(rows):
    return {r["id"]: r for r in rows}


def test_doc_id_normalization():
    assert doc_id("eval/corpus/dept-a/x.md") == "dept-a/x.md"
    assert doc_id("C:\\w\\eval\\corpus\\dept-a\\x.md") == "dept-a/x.md"
    assert doc_id("dept-a/x.md") == "dept-a/x.md"


def test_build_is_resumable_and_writes_gold_map(tmp_path):
    corpus = write_corpus(tmp_path)
    settings = settings_for(tmp_path / "snap")
    llm = FakeLLM(responder)
    c1 = build(corpus, settings, llm, FakeEmbedder())
    assert c1["files"] == 4 and c1["done"] == 4 and c1["articles"] == 4 and c1["embedded"] == 4
    n_calls = len(llm.calls)
    c2 = build(corpus, settings, llm, FakeEmbedder())
    assert c2["skipped"] == 4 and c2["done"] == 0 and len(llm.calls) == n_calls  # nothing re-ingested
    gm = GoldMap.load(settings.data_dir)
    assert set(gm.docs) == {"dept-a/alpha.md", "dept-a/beta.md", "dept-a/delta.md", "dept-b/gamma.md"}
    a = gm.docs["dept-a/alpha.md"]
    assert a["space"] == "dept-a" and a["raw_paths"][0].startswith("raw/dept-a/") and len(a["articles"]) == 1
    assert (corpus / "dept-a" / "alpha.md").is_file()  # the corpus is never moved/modified
    db = sqlite3.connect(str(settings.db_path))
    raw = db.execute("SELECT space FROM raw_files WHERE raw_path = ?", (a["raw_paths"][0],)).fetchone()
    assert raw == ("dept-a",)


def test_retrieval_eval_scores_and_acl(tmp_path):
    h = make_harness(tmp_path)
    rows = rows_by_id(h.run(BM25, "retrieval"))
    assert rows["q1"]["hits"] and rows["q1"]["recall"] == 1.0 and rows["q1"]["mrr"] == 1.0 and rows["q1"]["hit1"] == 1.0
    assert rows["q2"]["recall"] == 1.0
    assert rows["q3"]["empty"] and rows["q3"]["hits"] == []  # no token overlap -> nothing retrieved
    assert rows["q6"]["recall"] == 1.0  # dept-b user sees dept-b's doc
    # ACL: the dept-a asker for the gamma question never gets a dept-b article, so there is no leak
    assert all(not r["leak"] for r in rows.values())
    s = summarize(list(rows.values()), "retrieval")
    assert s["leaks"] == 0 and s["by_type"]["unanswerable"]["empty_rate"] == 1.0
    assert 0 < s["objective"] <= 1


def test_answer_eval_correctness_and_refusal(tmp_path):
    h = make_harness(tmp_path, echo_llm())
    rows = rows_by_id(h.run(BM25, "answer"))
    assert rows["q1"]["correct"] and rows["q1"]["fact_recall"] == 1.0 and rows["q1"]["citation_ok"]
    assert rows["q2"]["correct"]  # '45 분' vs '45분'
    assert rows["q3"]["correct"] and rows["q3"]["refused"]  # empty retrieval short-circuits to 근거 없음
    assert rows["q6"]["correct"]  # '21억3000만원' vs '21억 3,000만원'
    assert rows["q1"]["latency_ms"] > 0
    s = summarize(list(rows.values()), "answer")
    assert s["overall"]["latency_ms"]["p95"] >= s["overall"]["latency_ms"]["p50"] > 0


def test_planted_cross_space_leak_is_flagged(tmp_path):
    # a misbehaving LLM that blurts out the other space's fact for a question whose retrieval is non-empty
    llm = FakeLLM(lambda system, prompt: "The profit is 21억 3,000만원 [1]")
    h = make_harness(tmp_path, llm)
    q4 = next(q for q in h.qa if q.id == "q4")
    row = h.run(RetrievalParams(retrieval_mode="bm25", top_k=3), "answer", [q4])[0]
    # q4's gamma words do not retrieve dept-a articles with bm25 => force a non-empty retrieval via a shared word
    if row["empty"]:
        row = h.run(RetrievalParams(retrieval_mode="dense", top_k=3), "answer", [q4])[0]
    assert not row["empty"] and row["leak"] and "foreign fact in answer" in row["leak"][0]
    s = summarize([row], "answer")
    assert s["leaks"] == 1


def test_planted_leak_via_foreign_citation(tmp_path):
    h = make_harness(tmp_path)
    q = next(q for q in h.qa if q.id == "q1")  # asked from dept-a
    foreign = next(p for p in h.store.article_paths() if h.store.article_spaces(p) == frozenset({"dept-b"}))
    assert h.leak_reasons(q, "ok", [foreign])  # a dept-b article cited for a dept-a user


def test_foreign_facts_exclude_shared_facts(tmp_path):
    h = make_harness(tmp_path)
    ff = h.foreign_facts("dept-a")
    assert "21억 3,000만원" in ff and "21억3000만원" in ff
    assert "1200만원" not in ff and "March" not in ff  # dept-a's own facts are never foreign for dept-a


def test_llm_error_propagates_not_scored(tmp_path):
    class Down:
        def complete(self, system, prompt, temperature=None):
            raise LLMError("down")
    h = make_harness(tmp_path, Down())
    with pytest.raises(LLMError):
        h.run(BM25, "answer", [q for q in h.qa if q.id == "q1"])


def test_llm_cache_hits_and_key(tmp_path):
    inner = FakeLLM(lambda s, p: "ans:" + p)
    c = CachedLLM(inner, tmp_path / "c.sqlite", "m1")
    assert c.complete("s", "p", 0.0) == "ans:p" and c.complete("s", "p", 0.0) == "ans:p"
    assert len(inner.calls) == 1 and c.hits == 1 and c.misses == 1
    c.complete("s", "p", 0.5)  # temperature is part of the key
    c.complete("s", "p2", 0.0)
    assert len(inner.calls) == 3
    c.close()
    inner2 = FakeLLM(lambda s, p: "different")
    c2 = CachedLLM(inner2, tmp_path / "c.sqlite", "m1")  # persisted across processes
    assert c2.complete("s", "p", 0.0) == "ans:p" and not inner2.calls
    assert c2.saved_ms >= 0
    c3 = CachedLLM(inner2, tmp_path / "c.sqlite", "other-model")  # model is part of the key
    assert c3.complete("s", "p", 0.0) == "different"


def test_llm_cache_does_not_store_errors(tmp_path):
    class Flaky:
        n = 0

        def complete(self, system, prompt, temperature=None):
            self.n += 1
            if self.n == 1:
                raise LLMError("boom")
            return "ok"
    c = CachedLLM(Flaky(), tmp_path / "c.sqlite", "m")
    with pytest.raises(LLMError):
        c.complete("s", "p")
    assert c.complete("s", "p") == "ok"


def test_cached_answer_run_reuses_llm_calls(tmp_path):
    inner = echo_llm()
    h = make_harness(tmp_path, CachedLLM(inner, tmp_path / "c.sqlite", "m"))
    h.run(BM25, "answer")
    n = len(inner.calls)
    assert n > 0
    h.run(BM25, "answer")
    assert len(inner.calls) == n  # second run is served entirely from the cache


def test_heldout_guard(tmp_path):
    h = make_harness(tmp_path)
    cfg = {"retrieval_mode": "bm25", "top_k": 3}
    res = tmp_path / "results"
    rep = run_final(h, cfg, res, ("retrieval",))
    held = rep["modes"]["retrieval"]["heldout"]
    assert held["by_type"]["lookup"]["n"] == 1 and rep["n_heldout"] == 2 and "overall" in rep["modes"]["retrieval"]["gap"]
    assert (res / f"final-{rep['config_hash']}.json").is_file()
    with pytest.raises(HeldoutGuardError):
        run_final(h, cfg, res, ("retrieval",))
    # an equivalent spelling of the same config (defaults spelled out) has the same hash
    default_rrf_k = RetrievalParams().rrf_k
    assert params_hash({**cfg, "rrf_k": default_rrf_k}) == params_hash(cfg)
    assert params_hash({**cfg, "rrf_k": default_rrf_k + 1}) != params_hash(cfg)
    with pytest.raises(HeldoutGuardError):
        run_final(h, {**cfg, "rrf_k": default_rrf_k}, res, ("retrieval",))
    run_final(h, cfg, res, ("retrieval",), force=True)
    run_final(h, {"retrieval_mode": "bm25", "top_k": 4}, res, ("retrieval",))  # a different config is fine


def test_load_qa_validates(tmp_path):
    p = tmp_path / "q.jsonl"
    p.write_text(json.dumps({"id": "x", "space": "a", "question": "q", "type": "bogus", "split": "tune"}) + "\n")
    with pytest.raises(ValueError):
        load_qa(p)
    _, qa = make_snapshot(tmp_path / "s")
    assert {q.id for q in load_qa(qa, "heldout")} == {"q5", "q6"}
    assert len(load_qa(qa, "all")) == 6
