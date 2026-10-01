"""`report`: render docs-ready Markdown from eval/results (trial logs + final reports)."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from ..engine.params import RetrievalParams
from .dataset import TYPES
from .tuner import TrialLog, best_records

STAGES = ("retrieval", "generation")
CAVEATS = (
    "The corpus and the QA set are synthetic (generated), small, and written in one voice; real intranet "
    "documents are messier, so absolute scores are optimistic and only the relative ordering of configs is informative.",
    "Results come from a small local model (and a small embedding model). A larger production model may prefer "
    "different generation parameters; re-run the generation stage when the model changes.",
    "The tune split is small, so the objective is noisy and several configs are statistically tied; prefer "
    "the simplest config within noise of the best, and trust the held-out numbers over the tune numbers.",
    "The held-out split is evaluated once per config (guarded); the overfit gap is a single estimate, not a confidence interval.",
    "Retrieval is scored at article level: wiki articles may merge several source documents, so a hit can be "
    "credited for a gold document through a merged article.",
    "Cross-space leaks must be 0; any leak makes a config invalid regardless of its score.",
)


def _f(x, nd: int = 3) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def _table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def sensitivity(recs: list[dict]) -> dict[str, list[tuple[object, float, int]]]:
    """param -> [(value, best objective among non-rejected trials with that value, n trials)]."""
    ok = [r for r in recs if not r.get("rejected")]
    out: dict[str, list] = {}
    names = sorted({k for r in ok for k in r["cfg"]})
    for n in names:
        by: dict[str, list[float]] = {}
        vals: dict[str, object] = {}
        for r in ok:
            if n in r["cfg"]:
                key = json.dumps(r["cfg"][n])
                vals[key] = r["cfg"][n]
                by.setdefault(key, []).append(r["score"])
        if len(by) > 1:  # a parameter with a single value tells nothing
            out[n] = sorted(((vals[k], max(v), len(v)) for k, v in by.items()),
                            key=lambda t: (str(type(t[0])), t[0] if isinstance(t[0], (int, float)) else str(t[0])))
    return out


def _stage_section(stage: str, recs: list[dict]) -> list[str]:
    if not recs:
        return []
    ok = [r for r in recs if not r.get("rejected")]
    s = [f"## {stage.capitalize()} stage", "",
         f"{len(recs)} trials ({len(recs) - len(ok)} rejected for cross-space leaks).", ""]
    s += ["### Parameter sensitivity (best objective per value)", ""]
    for n, rows in sensitivity(recs).items():
        s += [f"**{n}**", "", _table(["value", "best objective", "trials"], [[str(v), _f(b, 4), str(c)] for v, b, c in rows]), ""]
    s += ["### Top-10 configs", ""]
    top = best_records(recs, 10)
    keys = sorted({k for r in top for k in r["cfg"]})
    s += [_table(["#", "objective", *keys], [[str(i), _f(r["score"], 4), *[str(r["cfg"].get(k, "")) for k in keys]]
                                              for i, r in enumerate(top, 1)]), ""]
    return s


def _chosen(recs_by_stage: dict[str, list[dict]]) -> list[str]:
    best = best_records(recs_by_stage.get("generation", []) or recs_by_stage.get("retrieval", []), 1)
    if not best:
        return []
    params = best[0]["params"]
    default = dataclasses.asdict(RetrievalParams())
    rows = [[k, str(default[k]), str(params.get(k)), "" if params.get(k) == default[k] else "changed"] for k in default]
    return ["## Chosen defaults", "", f"Best non-rejected config (objective {_f(best[0]['score'], 4)}) vs the shipped defaults.", "",
            _table(["parameter", "shipped", "chosen", ""], rows), ""]


def _final_section(report: dict) -> list[str]:
    s = ["## Held-out validation (tune vs heldout)", "",
         f"Config `{report['config_hash']}`; {report['n_tune']} tune / {report['n_heldout']} heldout questions. "
         "Gap = tune - heldout (positive = overfitting).", ""]
    for mode, m in report["modes"].items():
        gap = m["gap"]
        order = [t for t in TYPES if t in gap] + ["overall"]
        s += [f"### {mode} mode", "",
              _table(["type", "tune", "heldout", "gap"], [[t, _f(gap[t]["tune"]), _f(gap[t]["heldout"]), _f(gap[t]["gap"])] for t in order]), ""]
        leaks = m["heldout"]["leaks"] + m["tune"]["leaks"]
        s += [f"Cross-space leaks: **{leaks}**" + ("  (HARD FAIL)" if leaks else ""), ""]
        if mode == "answer":
            o = m["heldout"]["overall"]
            lat = o["latency_ms"]
            s += [f"Held-out latency p50 {lat['p50']:.0f} ms, p95 {lat['p95']:.0f} ms.", ""]
    return s


def render(results_dir: Path | str) -> str:
    rd = Path(results_dir)
    recs = {st: TrialLog(rd / f"tune-{st}.jsonl").load() for st in STAGES}
    finals = sorted(rd.glob("final-*.json"), key=lambda p: p.stat().st_mtime)
    out = ["# RAG evaluation and tuning report", ""]
    if finals:
        out += _final_section(json.loads(finals[-1].read_text(encoding="utf-8")))
    for st in STAGES:
        out += _stage_section(st, recs[st])
    out += _chosen(recs)
    if not finals and not any(recs.values()):
        out += ["_No tuning logs or final reports found in " + str(rd) + "._", ""]
    out += ["## Caveats", ""] + [f"- {c}" for c in CAVEATS] + [""]
    return "\n".join(out)
