"""Resumable parameter search: coarse grid (sampled) then local refinement (coordinate hill-climb).

The search is a pure function of (space, seed, trials, results): a restarted run proposes exactly the same
configs in the same order, so configs already in the JSONL trial log are replayed from it (never
re-evaluated) and the search continues where it was interrupted. `evaluate(cfg) -> dict` is injected,
so the tuner is independent of the RAG stack (and unit-testable on a toy objective).

Search space JSON (override with --space-file):
    {"strategy": "coarse_to_fine" | "grid" | "random",
     "params": {"top_k": [3, 5, 8], "w_bm25": [0.5, 1, 2], "retrieval_mode": ["bm25", "hybrid"],
                "min_cosine": {"float": [0.0, 0.6]}, "candidate_pool": {"int": [20, 100]},
                "x": {"choice": ["a", "b"]}},
     "fixed": {"rrf_k": 60}}
A bare {"param": [values]} mapping is accepted as coarse_to_fine. List/choice values are the coarse grid;
{"int"|"float": [lo, hi]} ranges give a coarse grid of a few evenly spaced points and also bound refinement.
Non-numeric lists are categorical (refinement tries every other value; numeric lists also try midpoints).
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RETRIEVAL_SPACE: dict = {
    "strategy": "coarse_to_fine",
    "params": {
        "retrieval_mode": ["bm25", "dense", "hybrid"],
        "top_k": [3, 5, 8, 12],
        "candidate_pool": [20, 50, 100],
        "rrf_k": [10, 30, 60, 100],
        "w_bm25": [0.5, 1.0, 2.0],
        "w_dense": [0.5, 1.0, 2.0],
        "min_cosine": [0.0, 0.2, 0.35, 0.5],
        "bm25_k1": [0.9, 1.5, 2.0],
        "bm25_b": [0.4, 0.75, 0.9],
    },
}
DEFAULT_GENERATION_SPACE: dict = {
    "strategy": "coarse_to_fine",
    "params": {
        "temperature": [0.0, 0.2, 0.5],
        "top_k": [2, 3, 5, 8],
        "max_context_chars": [0, 1500, 3000],
    },
}
_DEFAULTS = {"rrf_k": 60, "w_bm25": 1.0, "w_dense": 1.0, "min_cosine": 0.0, "bm25_k1": 1.5, "bm25_b": 0.75}


def canon_retrieval(cfg: dict) -> dict:
    """Reset knobs that cannot influence the chosen retrieval_mode, so equivalent configs dedupe."""
    c = dict(cfg)
    mode = c.get("retrieval_mode", "hybrid")
    drop: tuple[str, ...] = ()
    if mode == "bm25":
        drop = ("rrf_k", "w_bm25", "w_dense", "min_cosine")
    elif mode == "dense":
        drop = ("rrf_k", "w_bm25", "w_dense", "bm25_k1", "bm25_b")
    for k in drop:
        if k in c:
            c[k] = _DEFAULTS[k]
    return c


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


@dataclass
class Param:
    values: list
    numeric: bool
    integer: bool = False
    lo: float | None = None
    hi: float | None = None
    ranged: bool = False


def _round(v: float, integer: bool):
    return int(round(v)) if integer else float(f"{v:.4g}")


def parse_param(spec) -> Param:
    if isinstance(spec, dict):
        if "choice" in spec:
            return Param(list(spec["choice"]), False)
        for kind in ("int", "float"):
            if kind in spec:
                lo, hi = spec[kind]
                n = 4 if kind == "int" else 3
                vals = sorted({_round(lo + (hi - lo) * i / (n - 1), kind == "int") for i in range(n)})
                return Param(vals, True, kind == "int", lo, hi, True)
        raise ValueError(f"unknown param spec {spec!r}")
    vals = list(spec)
    if not vals:
        raise ValueError("empty value list")
    numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)
    if numeric:
        vals = sorted(set(vals))
    return Param(vals, numeric, numeric and all(isinstance(v, int) for v in vals))


def parse_space(spec: dict) -> tuple[str, dict[str, Param], dict]:
    if "params" not in spec:
        spec = {"params": spec}
    strategy = spec.get("strategy", "coarse_to_fine")
    if strategy not in ("coarse_to_fine", "grid", "random"):
        raise ValueError(f"unknown strategy {strategy!r}")
    return strategy, {k: parse_param(v) for k, v in spec["params"].items()}, dict(spec.get("fixed", {}))


def _sample(p: Param, rng: random.Random):
    if p.ranged:
        return _round(rng.uniform(p.lo, p.hi), p.integer)
    return rng.choice(p.values)


def _neighbors(p: Param, cur) -> list:
    vals = p.values
    if not p.numeric:
        return [v for v in vals if v != cur]
    out: list = []
    below = [v for v in vals if v < cur]
    above = [v for v in vals if v > cur]
    step = (vals[-1] - vals[0]) / max(len(vals) - 1, 1)
    for direction, nb in ((-1, max(below) if below else None), (1, min(above) if above else None)):
        if nb is not None:
            out += [nb, _round((cur + nb) / 2, p.integer)]
        elif p.ranged:  # at the edge of a ranged param: probe one step beyond (bounds are applied below)
            out.append(_round(cur + direction * step, p.integer))
    return [v for v in dict.fromkeys(out) if v != cur and (p.lo is None or v >= p.lo) and (p.hi is None or v <= p.hi)]


class TrialLog:
    """Append-only JSONL, one line per trial, fsync'd so an interruption loses at most the running trial."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:  # a torn last line from a hard kill: ignore, it is re-evaluated
                    continue
        return out

    def append(self, rec: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def best_records(recs: list[dict], n: int = 1) -> list[dict]:
    ok = [r for r in recs if not r.get("rejected")]
    return sorted(ok, key=lambda r: (-r["score"], r.get("trial", 0)))[:n]


def search(space: dict, evaluate: Callable[[dict], dict], log: TrialLog, trials: int, seed: int = 0,
           stage: str = "retrieval", canon: Callable[[dict], dict] = lambda c: c,
           on_trial: Callable[[dict], None] | None = None) -> list[dict]:
    """Run up to `trials` distinct configs (replayed log entries count). Returns this run's trial records."""
    strategy, params, fixed = parse_space(space)
    rng = random.Random(seed)
    seen = {r["hash"]: r for r in log.load() if r.get("stage") == stage and r.get("seed", seed) == seed}
    done: dict[str, dict] = {}
    order = list(params)

    def run(cfg: dict) -> dict | None:
        cfg = canon({**fixed, **cfg})
        h = config_hash(cfg)
        if h in done:
            return done[h]
        if len(done) >= trials:
            return None
        rec = seen.get(h)
        if rec is None:
            res = evaluate(cfg)
            rec = {"stage": stage, "seed": seed, "hash": h, "cfg": cfg, **res, "ts": time.time()}
            rec["trial"] = len(done) + 1
            log.append(rec)
            if on_trial:
                on_trial(rec)
        done[h] = rec
        return rec

    def score(r: dict) -> float:
        return -math.inf if r.get("rejected") else r["score"]

    n_coarse = trials if strategy != "coarse_to_fine" else max(1, math.ceil(trials * 0.6))
    size = math.prod(len(p.values) for p in params.values())
    if strategy == "random":
        for _ in range(trials * 20):
            if run({k: _sample(p, rng) for k, p in params.items()}) is None:
                break
    else:
        if size <= max(n_coarse, 1):
            cands = [dict(zip(order, combo)) for combo in itertools.product(*(params[k].values for k in order))]
        else:  # sample the grid without materializing it; dedupe by config hash through `run`
            cands = ({k: rng.choice(p.values) for k, p in params.items()} for _ in range(n_coarse * 20))
        for cfg in cands:
            if len(done) >= n_coarse or run(cfg) is None:
                break
    if strategy == "coarse_to_fine" and done:
        best = max(done.values(), key=lambda r: (score(r), -r["trial"]))
        if score(best) > -math.inf:
            improved = True
            while improved and len(done) < trials:
                improved = False
                names = order[:]
                rng.shuffle(names)
                for name in names:
                    for v in _neighbors(params[name], best["cfg"].get(name, params[name].values[0])):
                        r = run({**best["cfg"], name: v})
                        if r is None:
                            break
                        if score(r) > score(best):
                            best, improved = r, True
                    if len(done) >= trials:
                        break
    return list(done.values())
