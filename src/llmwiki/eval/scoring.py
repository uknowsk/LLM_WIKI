"""Aggregation of per-question rows into summaries and tuning objectives."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from .dataset import ANSWERABLE, REFUSAL_TYPES, TYPES
from .metrics import latency_stats


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def retrieval_score(row: dict) -> float:
    """Per-question retrieval quality for answerable questions."""
    return 0.5 * row["recall"] + 0.3 * row["mrr"] + 0.2 * row["ndcg"]


def _blend(a: Sequence[float], b: Sequence[float]) -> float:
    """0.7*mean(a) + 0.3*mean(b); a missing group hands its weight to the other."""
    if a and b:
        return 0.7 * _mean(a) + 0.3 * _mean(b)
    return _mean(a) if a else _mean(b)


def retrieval_objective(rows: Sequence[dict]) -> float:
    ans = [retrieval_score(r) for r in rows if r["type"] in ANSWERABLE]
    ref = [1.0 if r["empty"] else 0.0 for r in rows if r["type"] in REFUSAL_TYPES]
    return _blend(ans, ref)


def generation_objective(rows: Sequence[dict]) -> float:
    ans = [1.0 if r["correct"] else 0.0 for r in rows if r["type"] in ANSWERABLE]
    ref = [1.0 if r["correct"] else 0.0 for r in rows if r["type"] in REFUSAL_TYPES]
    return _blend(ans, ref)


def leak_count(rows: Sequence[dict]) -> int:
    return sum(1 for r in rows if r.get("leak"))


def type_scores(rows: Sequence[dict], mode: str) -> dict[str, float]:
    """One headline number per question type. retrieval: composite (answerable) / empty rate (refusal types);
    answer: correct rate for every type (refusal accuracy for refusal types)."""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["type"]].append(r)
    out = {}
    for t in TYPES:
        rs = by.get(t)
        if not rs:
            continue
        if mode == "answer":
            out[t] = _mean([1.0 if r["correct"] else 0.0 for r in rs])
        elif t in ANSWERABLE:
            out[t] = _mean([retrieval_score(r) for r in rs])
        else:
            out[t] = _mean([1.0 if r["empty"] else 0.0 for r in rs])
    return out


def summarize(rows: Sequence[dict], mode: str) -> dict:
    """Overall + per-type summary. Leaks are reported separately and must be 0."""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["type"]].append(r)

    def block(rs: list[dict]) -> dict:
        ans = [r for r in rs if r["type"] in ANSWERABLE]
        ref = [r for r in rs if r["type"] in REFUSAL_TYPES]
        b: dict = {"n": len(rs)}
        if ans:
            for k in ("recall", "hit", "hit1", "mrr", "ndcg"):
                b[k] = _mean([r[k] for r in ans])
            b["retrieval_score"] = _mean([retrieval_score(r) for r in ans])
            if any("stale_top1" in r for r in ans):  # 'latest' questions: top hit is an outdated document
                b["stale_top1"] = _mean([1.0 if r.get("stale_top1") else 0.0 for r in ans if "stale_top1" in r])
            if mode == "answer":
                b["correct"] = _mean([1.0 if r["correct"] else 0.0 for r in ans])
                b["fact_recall"] = _mean([r["fact_recall"] for r in ans])
                b["citation_accuracy"] = _mean([1.0 if r["citation_ok"] else 0.0 for r in ans])
        if ref:
            b["empty_rate"] = _mean([1.0 if r["empty"] else 0.0 for r in ref])
            if mode == "answer":
                b["refusal_accuracy"] = _mean([1.0 if r["correct"] else 0.0 for r in ref])
        b["latency_ms"] = latency_stats([r["latency_ms"] for r in rs])
        b["leaks"] = leak_count(rs)
        return b

    return {
        "mode": mode,
        "overall": block(list(rows)),
        "by_type": {t: block(by[t]) for t in TYPES if t in by},
        "objective": retrieval_objective(rows) if mode == "retrieval" else generation_objective(rows),
        "retrieval_objective": retrieval_objective(rows),
        "leaks": leak_count(rows),
    }
