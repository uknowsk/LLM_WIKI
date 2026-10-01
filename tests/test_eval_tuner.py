import json

import pytest

from llmwiki.eval import cli, report, stages, tuner
from llmwiki.eval.dataset import load_qa
from test_eval_support import echo_llm, make_harness

SPACE = {"strategy": "coarse_to_fine", "params": {"x": [0, 2, 4, 8], "y": [0.0, 0.5, 1.0], "mode": ["a", "b"]}}


def toy(cfg):
    """Known optimum: x=3 (not on the coarse grid), y=0.5, mode=b."""
    score = -((cfg["x"] - 3) ** 2) - (cfg["y"] - 0.5) ** 2 + (0.5 if cfg["mode"] == "b" else 0)
    return {"score": score, "metrics": {}}


class Boom(Exception):
    pass


def test_tuner_finds_known_better_config_by_local_refinement(tmp_path):
    recs = tuner.search(SPACE, toy, tuner.TrialLog(tmp_path / "t.jsonl"), trials=40, seed=1)
    best = tuner.best_records(recs, 1)[0]
    assert best["cfg"] == {"x": 3, "y": 0.5, "mode": "b"}  # x=3 is only reachable through refinement
    coarse_best = max(r["score"] for r in recs[:15])
    assert best["score"] > coarse_best
    assert len({r["hash"] for r in recs}) == len(recs) <= 40


def test_grid_and_random_strategies(tmp_path):
    g = tuner.search({**SPACE, "strategy": "grid"}, toy, tuner.TrialLog(tmp_path / "g.jsonl"), trials=100)
    assert len(g) == 24
    r = tuner.search({"strategy": "random", "params": {"x": {"int": [0, 8]}, "y": {"float": [0, 1]}, "mode": {"choice": ["a", "b"]}}},
                     toy, tuner.TrialLog(tmp_path / "r.jsonl"), trials=15, seed=3)
    assert 10 <= len(r) <= 15 and all(0 <= x["cfg"]["x"] <= 8 for x in r)


def test_resume_after_kill_continues_without_repeating(tmp_path):
    full = tuner.search(SPACE, toy, tuner.TrialLog(tmp_path / "full.jsonl"), trials=30, seed=2)
    log = tuner.TrialLog(tmp_path / "t.jsonl")
    calls: list[str] = []

    def killer(cfg):
        if len(calls) == 7:
            raise Boom
        calls.append(tuner.config_hash(cfg))
        return toy(cfg)
    with pytest.raises(Boom):
        tuner.search(SPACE, killer, log, trials=30, seed=2)
    assert len(log.load()) == 7  # everything finished before the kill is on disk
    calls2: list[str] = []

    def counting(cfg):
        calls2.append(tuner.config_hash(cfg))
        return toy(cfg)
    resumed = tuner.search(SPACE, counting, log, trials=30, seed=2)
    assert not set(calls) & set(calls2)  # no config evaluated twice
    assert len(calls) + len(calls2) == len(resumed) == len(full)
    lines = [json.loads(x) for x in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert len({x["hash"] for x in lines}) == len(lines)
    assert [r["hash"] for r in resumed] == [r["hash"] for r in full]  # identical search trajectory
    # a finished log is a pure replay: zero evaluations
    assert tuner.search(SPACE, lambda c: pytest.fail("re-evaluated"), log, trials=30, seed=2)


def test_torn_last_log_line_is_ignored(tmp_path):
    log = tuner.TrialLog(tmp_path / "t.jsonl")
    tuner.search(SPACE, toy, log, trials=5, seed=0)
    with open(log.path, "a", encoding="utf-8") as f:
        f.write('{"stage": "retrieval", "hash": "abc", "sco')
    assert len(log.load()) == 5


def test_rejected_trials_never_win(tmp_path):
    def evaluate(cfg):
        bad = cfg["x"] == 4
        return {"score": -1.0 if bad else toy(cfg)["score"] - 100 * (cfg["x"] != 4), "rejected": bad}
    recs = tuner.search({"strategy": "grid", "params": {"x": [0, 2, 4], "y": [0.5], "mode": ["b"]}}, evaluate,
                        tuner.TrialLog(tmp_path / "t.jsonl"), trials=10)
    assert tuner.best_records(recs, 1)[0]["cfg"]["x"] != 4


def test_canon_dedupes_irrelevant_knobs():
    a = tuner.canon_retrieval({"retrieval_mode": "bm25", "w_dense": 2.0, "min_cosine": 0.5, "bm25_k1": 0.9})
    b = tuner.canon_retrieval({"retrieval_mode": "bm25", "w_dense": 0.5, "min_cosine": 0.0, "bm25_k1": 0.9})
    assert tuner.config_hash(a) == tuner.config_hash(b)
    d = tuner.canon_retrieval({"retrieval_mode": "dense", "bm25_k1": 0.9, "min_cosine": 0.3})
    assert d["bm25_k1"] == 1.5 and d["min_cosine"] == 0.3


def test_default_spaces_parse():
    for sp in (tuner.DEFAULT_RETRIEVAL_SPACE, tuner.DEFAULT_GENERATION_SPACE):
        _, params, _ = tuner.parse_space(sp)
        assert params
    assert set(tuner.DEFAULT_RETRIEVAL_SPACE["params"]) >= {"retrieval_mode", "top_k", "candidate_pool", "rrf_k",
                                                            "w_bm25", "w_dense", "min_cosine", "bm25_k1", "bm25_b"}
    assert set(tuner.DEFAULT_GENERATION_SPACE["params"]) == {"temperature", "top_k", "max_context_chars"}


SMALL = {"strategy": "coarse_to_fine", "params": {"retrieval_mode": ["bm25", "hybrid"], "top_k": [1, 3], "bm25_k1": [0.9, 1.5]}}


def test_retrieval_stage_uses_given_questions_only_and_rejects_leaks(tmp_path):
    h = make_harness(tmp_path)
    tune_q = [q for q in h.qa if q.split == "tune"]
    seen_ids = []
    orig = h.run

    def spy(params, mode, questions=None):
        seen_ids.extend(q.id for q in questions)
        return orig(params, mode, questions)
    h.run = spy
    recs = stages.tune("retrieval", h, tune_q, tmp_path / "r.jsonl", 8, 0, SMALL)
    assert set(seen_ids) == {"q1", "q2", "q3", "q4"}  # heldout questions are never evaluated while tuning
    best = tuner.best_records(recs, 1)[0]
    assert best["score"] > 0 and best["params"]["retrieval_mode"] in ("bm25", "hybrid")
    # a planted leak rejects the config
    h.leak_reasons = lambda q, a, p: ["planted leak"] if q.id == "q4" else []
    h.run = orig
    rec = stages.retrieval_evaluator(h, tune_q)({"retrieval_mode": "bm25"})
    assert rec["rejected"] and rec["score"] == -1.0 and "planted leak" in rec["reason"]


def test_generation_stage_refines_best_retrieval_configs(tmp_path):
    h = make_harness(tmp_path, echo_llm())
    tune_q = [q for q in h.qa if q.split == "tune"]
    stages.tune("retrieval", h, tune_q, tmp_path / "r.jsonl", 6, 0, SMALL)
    gen_space = {"params": {"temperature": [0.0, 0.5], "top_k": [2, 3], "max_context_chars": [0, 200]}}
    recs = stages.tune("generation", h, tune_q, tmp_path / "g.jsonl", 6, 0, gen_space, tmp_path / "r.jsonl", 2)
    assert recs and all("latency_p50_ms" in r["metrics"] for r in recs)
    assert all(r["params"]["retrieval_mode"] in ("bm25", "hybrid") for r in recs)
    assert {r["cfg"]["_base"] for r in recs} <= {0, 1}
    with pytest.raises(ValueError):
        stages.tune("generation", h, tune_q, tmp_path / "g2.jsonl", 3, 0)  # needs --from-results


def test_report_renders_tables(tmp_path):
    h = make_harness(tmp_path)
    tune_q = [q for q in h.qa if q.split == "tune"]
    res = tmp_path / "results"
    stages.tune("retrieval", h, tune_q, res / "tune-retrieval.jsonl", 8, 0, SMALL)
    md = report.render(res)
    assert "Parameter sensitivity" in md and "Top-10 configs" in md and "Chosen defaults" in md and "Caveats" in md
    assert "synthetic" in md and "small" in md.lower()
    from llmwiki.eval.final import run_final
    run_final(h, {"retrieval_mode": "bm25", "top_k": 3}, res, ("retrieval",))
    md = report.render(res)
    assert "Held-out validation" in md and "| overall |" in md and "lookup" in md


def test_sensitivity_best_per_value():
    recs = [{"cfg": {"a": 1}, "score": 0.2}, {"cfg": {"a": 1}, "score": 0.4}, {"cfg": {"a": 2}, "score": 0.3},
            {"cfg": {"a": 2}, "score": 0.9, "rejected": True}, {"cfg": {"b": 5}, "score": 0.1}]
    s = report.sensitivity(recs)
    assert s == {"a": [(1, 0.4, 2), (2, 0.3, 1)]}  # rejected excluded; single-valued params dropped


def test_cli_smoke(tmp_path, monkeypatch, capsys):
    from llmwiki.engine.embed import FakeEmbedder
    from llmwiki.engine.llm import FakeLLM
    from test_eval_support import write_corpus, write_qa
    from test_pipeline_support import responder
    monkeypatch.setattr(cli, "_make_llm", lambda settings, args: FakeLLM(responder) if args.cmd == "build" else echo_llm())
    monkeypatch.setattr(cli, "_make_embedder", lambda settings, args: FakeEmbedder())
    corpus, qa = write_corpus(tmp_path), write_qa(tmp_path)
    snap, res = str(tmp_path / "snap"), str(tmp_path / "res")
    assert cli.main(["build", "--corpus", str(corpus), "--out", snap]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["done"] == 4
    common = ["--data", snap, "--qa", str(qa), "--results", res]
    assert cli.main(["eval", *common, "--split", "tune", "--params", '{"retrieval_mode": "bm25"}']) == 0
    assert "OVERALL" in capsys.readouterr().out
    assert cli.main(["eval", *common, "--split", "all", "--mode", "answer", "--params", '{"retrieval_mode": "bm25"}']) == 0
    assert cli.main(["tune", *common, "--stage", "retrieval", "--trials", "6", "--seed", "1"]) == 0
    best = tmp_path / "res" / "best-retrieval.json"
    assert best.is_file()
    assert cli.main(["tune", *common, "--stage", "generation", "--trials", "3", "--from-results",
                     str(tmp_path / "res" / "tune-retrieval.jsonl"), "--top-n", "1"]) == 0
    capsys.readouterr()
    assert cli.main(["final", *common, "--config", str(best)]) == 0
    out = capsys.readouterr().out
    assert "tune vs heldout" in out and "overall" in out
    assert cli.main(["final", *common, "--config", str(best)]) == 4  # guard: second heldout run is refused
    assert cli.main(["final", *common, "--config", str(best), "--force"]) == 0
    capsys.readouterr()
    outmd = tmp_path / "report.md"
    assert cli.main(["report", "--results", res, "--out", str(outmd)]) == 0
    assert "# RAG evaluation and tuning report" in outmd.read_text(encoding="utf-8")
    assert len(load_qa(qa, "heldout")) == 2
