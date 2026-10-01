"""Tuning stages: bind the generic tuner to the RAG harness (retrieval stage, generation stage)."""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from pathlib import Path

from ..engine.params import RetrievalParams
from . import tuner
from .dataset import QA
from .runner import Harness
from .scoring import leak_count, summarize, type_scores

PARAM_FIELDS = {f.name for f in dataclasses.fields(RetrievalParams)}


def make_params(d: dict) -> RetrievalParams:
    bad = set(d) - PARAM_FIELDS
    if bad:
        raise ValueError(f"unknown RetrievalParams fields: {sorted(bad)}")
    return RetrievalParams(**d)


def params_dict(p: RetrievalParams) -> dict:
    return dataclasses.asdict(p)


def compact(summary: dict, rows: Sequence[dict], mode: str) -> dict:
    o = summary["overall"]
    keys = ("recall", "hit1", "mrr", "ndcg", "empty_rate", "correct", "fact_recall", "citation_accuracy", "refusal_accuracy")
    out = {k: round(o[k], 4) for k in keys if k in o}
    out["latency_p50_ms"] = round(o["latency_ms"]["p50"], 1)
    out["latency_p95_ms"] = round(o["latency_ms"]["p95"], 1)
    out["leaks"] = summary["leaks"]
    out["by_type"] = {t: round(v, 4) for t, v in type_scores(rows, mode).items()}
    return out


def _result(rows: list[dict], mode: str, params: RetrievalParams) -> dict:
    s = summarize(rows, mode)
    leaks = leak_count(rows)
    res = {"score": -1.0 if leaks else round(s["objective"], 6), "params": params_dict(params),
           "metrics": compact(s, rows, mode), "rejected": bool(leaks)}
    if leaks:
        res["reason"] = f"cross-space leak in {leaks} question(s): " + "; ".join(
            f"{r['id']}: {r['leak'][0]}" for r in rows if r.get("leak"))[:400]
    return res


def retrieval_evaluator(h: Harness, questions: Sequence[QA]) -> Callable[[dict], dict]:
    """LLM-free: cfg is a RetrievalParams override dict."""
    def evaluate(cfg: dict) -> dict:
        p = make_params(cfg)
        return _result(h.run(p, "retrieval", questions), "retrieval", p)
    return evaluate


def generation_evaluator(h: Harness, questions: Sequence[QA], bases: list[dict]) -> Callable[[dict], dict]:
    """cfg = generation overrides (+ `_base`: index into `bases`, the retrieval configs being refined)."""
    def evaluate(cfg: dict) -> dict:
        merged = {**bases[int(cfg.get("_base", 0))], **{k: v for k, v in cfg.items() if k != "_base"}}
        p = make_params(merged)
        return _result(h.run(p, "answer", questions), "answer", p)
    return evaluate


def generation_space(base_space: dict, n_bases: int) -> dict:
    strategy, _, _ = tuner.parse_space(base_space)
    spec = base_space if "params" in base_space else {"params": base_space}
    params = {**spec["params"], "_base": {"choice": list(range(n_bases))}}
    return {**spec, "strategy": strategy, "params": params}


def tune(stage: str, h: Harness, questions: Sequence[QA], log_path: Path | str, trials: int, seed: int,
         space: dict | None = None, from_results: Path | str | None = None, top_n: int = 3,
         on_trial: Callable[[dict], None] | None = None) -> list[dict]:
    if stage == "retrieval":
        return tuner.search(space or tuner.DEFAULT_RETRIEVAL_SPACE, retrieval_evaluator(h, questions),
                            tuner.TrialLog(log_path), trials, seed, "retrieval", tuner.canon_retrieval, on_trial)
    if stage != "generation":
        raise ValueError("stage must be retrieval|generation")
    if not from_results:
        raise ValueError("--from-results (a retrieval trial log) is required for the generation stage")
    top = tuner.best_records(tuner.TrialLog(from_results).load(), top_n)
    if not top:
        raise ValueError(f"no usable (non-rejected) trials in {from_results}")
    bases = [r["params"] for r in top]
    return tuner.search(generation_space(space or tuner.DEFAULT_GENERATION_SPACE, len(bases)),
                        generation_evaluator(h, questions, bases), tuner.TrialLog(log_path), trials, seed,
                        "generation", lambda c: c, on_trial)
