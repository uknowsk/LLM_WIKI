"""CLI: python -m llmwiki.eval {build,eval,tune,final,report}."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from ..config import Settings
from ..engine.embed import Embedder, embedder_from_env
from ..engine.llm import LLMClient, OpenAICompatClient
from . import report as report_mod
from .build import build
from .dataset import TYPES, load_qa
from .final import HeldoutGuardError, load_config, run_final
from .llmcache import CachedLLM
from .runner import Harness, settings_for
from .scoring import summarize
from .stages import make_params, params_dict, tune
from .tuner import TrialLog, best_records

DEFAULT_RESULTS = "eval/results"


def _make_llm(settings: Settings, args) -> LLMClient:  # patched in tests
    return OpenAICompatClient(settings.llm_base_url, settings.llm_model, timeout=args.timeout)


def _make_embedder(settings: Settings, args) -> Embedder | None:  # patched in tests
    if args.no_embed:
        return None
    env = dict(os.environ)
    if args.embed_model:
        env["WIKI_EMBED_MODEL"] = args.embed_model
    return embedder_from_env(settings, env)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--llm-model", default=None, help="chat model (default: WIKI_LLM_MODEL)")
    p.add_argument("--base-url", default=None, help="OpenAI-compatible endpoint (default: WIKI_LLM_BASE_URL)")
    p.add_argument("--embed-model", default=None, help="embedding model (default: WIKI_EMBED_MODEL)")
    p.add_argument("--no-embed", action="store_true", help="BM25 only (no embedder)")
    p.add_argument("--timeout", type=float, default=300.0, help="LLM request timeout in seconds")


def _data_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", required=True, help="data dir snapshot made by `build`")
    p.add_argument("--qa", default="eval/qa.jsonl")
    p.add_argument("--results", default=DEFAULT_RESULTS, help="results dir (reports, trial logs, llm cache)")
    _common(p)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m llmwiki.eval", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="ingest the corpus into a reusable data dir (resumable)")
    b.add_argument("--corpus", default="eval/corpus")
    b.add_argument("--out", required=True)
    _common(b)
    e = sub.add_parser("eval", help="evaluate one config on a split")
    _data_args(e)
    e.add_argument("--split", choices=("tune", "heldout", "all"), default="tune")
    e.add_argument("--params", default="{}", help="JSON RetrievalParams overrides")
    e.add_argument("--mode", choices=("retrieval", "answer"), default="retrieval")
    t = sub.add_parser("tune", help="search parameters on the tune split (resumable)")
    _data_args(t)
    t.add_argument("--stage", choices=("retrieval", "generation"), required=True)
    t.add_argument("--space-file", default=None, help="JSON search space (default: built-in coarse-to-fine)")
    t.add_argument("--trials", type=int, default=200)
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--log", default=None, help="JSONL trial log (default: <results>/tune-<stage>.jsonl)")
    t.add_argument("--from-results", default=None, help="generation stage: retrieval trial log to take the best configs from")
    t.add_argument("--top-n", type=int, default=3, help="generation stage: how many retrieval configs to refine")
    f = sub.add_parser("final", help="evaluate one chosen config ONCE on the heldout split")
    _data_args(f)
    f.add_argument("--config", required=True, help="JSON file: params dict, trial record, or best-*.json")
    f.add_argument("--mode", choices=("retrieval", "answer", "both"), default="both")
    f.add_argument("--force", action="store_true", help="re-run a config already evaluated on heldout")
    r = sub.add_parser("report", help="render Markdown from the results dir")
    r.add_argument("--results", default=DEFAULT_RESULTS)
    r.add_argument("--out", default=None)
    return ap


def _pct(x) -> str:
    return "  -  " if x is None else f"{x:5.3f}"


def _print_summary(s: dict) -> None:
    out = sys.stdout
    mode = s["mode"]
    cols = ("recall", "hit1", "mrr", "ndcg", "empty_rate") if mode == "retrieval" else (
        "correct", "fact_recall", "citation_accuracy", "refusal_accuracy", "recall", "empty_rate")
    print(f"{'type':14}{'n':>4} " + " ".join(f"{c[:11]:>11}" for c in cols) + "   p50ms   p95ms  leaks", file=out)
    for t, b in [*s["by_type"].items(), ("OVERALL", s["overall"])]:
        lat = b["latency_ms"]
        print(f"{t:14}{b['n']:>4} " + " ".join(f"{_pct(b.get(c)):>11}" for c in cols)
              + f" {lat['p50']:7.0f} {lat['p95']:7.0f} {b['leaks']:6d}", file=out)
    print(f"objective({mode}) = {s['objective']:.4f}", file=out)
    if s["leaks"]:
        banner_leak(s["leaks"])


def banner_leak(n: int) -> None:
    print("\n" + "!" * 70 + f"\n!!! CROSS-SPACE LEAK DETECTED in {n} question(s) -- HARD FAIL (must be 0) !!!\n" + "!" * 70,
          file=sys.stderr)


def _open(args, need_llm: bool, qa) -> Harness:
    settings = settings_for(args.data, args.base_url, args.llm_model)
    emb = _make_embedder(settings, args)
    llm = None
    if need_llm:
        results = Path(args.results)
        results.mkdir(parents=True, exist_ok=True)
        llm = CachedLLM(_make_llm(settings, args), results / "llm_cache.sqlite", settings.llm_model)
    h = Harness(settings, qa, llm, emb)
    if emb is not None and h.n_embedded == 0:
        print("WARNING: the snapshot has no stored embeddings; dense/hybrid retrieval degrades to BM25.", file=sys.stderr)
    return h


def _warn_ambiguous(h: Harness) -> None:
    if h.ambiguous_leak_facts:
        ids = ", ".join(sorted({i for i, _ in h.ambiguous_leak_facts}))
        print(f"WARNING: leak_facts that also occur in the asking space's own articles are ignored by the leak "
              f"detector (dataset ambiguity): {ids}", file=sys.stderr)


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_build(args) -> int:
    settings = settings_for(args.out, args.base_url, args.llm_model)
    llm = _make_llm(settings, args)
    counts = build(args.corpus, settings, llm, _make_embedder(settings, args))
    print(json.dumps(counts, ensure_ascii=False))
    return 2 if counts.get("aborted") else 0


def cmd_eval(args) -> int:
    qa = load_qa(args.qa, "all")
    h = _open(args, args.mode == "answer", qa)
    try:
        params = make_params(json.loads(args.params))
        rows = h.run(params, args.mode, [q for q in qa if args.split in ("all", q.split)])
        s = summarize(rows, args.mode)
        path = Path(args.results) / f"eval-{args.mode}-{args.split}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        _write_json(path, {"params": params_dict(params), "split": args.split, "summary": s, "rows": rows})
        _print_summary(s)
        _warn_ambiguous(h)
        print(f"report: {path}")
        return 3 if s["leaks"] else 0
    finally:
        h.close()


def cmd_tune(args) -> int:
    qa = load_qa(args.qa, "tune")  # tuning NEVER sees the heldout split
    space = json.loads(Path(args.space_file).read_text(encoding="utf-8")) if args.space_file else None
    log = args.log or str(Path(args.results) / f"tune-{args.stage}.jsonl")
    h = _open(args, args.stage == "generation", load_qa(args.qa, "all"))  # all: foreign-fact universe only
    try:
        def on_trial(r: dict) -> None:
            print(f"trial {r['trial']:4d} score={r['score']:.4f}{' REJECTED' if r.get('rejected') else ''} "
                  f"{json.dumps(r['cfg'], ensure_ascii=False)}", file=sys.stderr)
        recs = tune(args.stage, h, qa, log, args.trials, args.seed, space, args.from_results, args.top_n, on_trial)
    finally:
        h.close()
    best = best_records(recs, 1)
    rejected = sum(1 for r in recs if r.get("rejected"))
    print(f"{len(recs)} trials ({rejected} rejected for leaks); log: {log}")
    if best:
        _write_json(Path(args.results) / f"best-{args.stage}.json", {"params": best[0]["params"], "score": best[0]["score"]})
        print(f"best score {best[0]['score']:.4f}: {json.dumps(best[0]['params'], ensure_ascii=False)}")
    return 0


def cmd_final(args) -> int:
    qa = load_qa(args.qa, "all")
    modes = ("retrieval", "answer") if args.mode == "both" else (args.mode,)
    cfg = load_config(args.config)
    h = _open(args, "answer" in modes, qa)
    try:
        rep = run_final(h, cfg, args.results, modes, args.force)
    except HeldoutGuardError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 4
    finally:
        _warn_ambiguous(h)
        h.close()
    leaks = 0
    for mode, m in rep["modes"].items():
        print(f"\n== {mode}: tune vs heldout (gap = tune - heldout) ==")
        print(f"{'type':14}{'tune':>8}{'heldout':>9}{'gap':>8}")
        for t in [*[t for t in TYPES if t in m["gap"]], "overall"]:
            g = m["gap"][t]
            print(f"{t:14}{_pct(g['tune']):>8}{_pct(g['heldout']):>9}{_pct(g['gap']):>8}")
        leaks += m["tune"]["leaks"] + m["heldout"]["leaks"]
    if leaks:
        banner_leak(leaks)
    print(f"config {rep['config_hash']}: report {Path(args.results) / ('final-' + rep['config_hash'] + '.json')}")
    return 3 if leaks else 0


def cmd_report(args) -> int:
    md = report_mod.render(args.results)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(md)
    return 0


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = parser().parse_args(argv)
    return {"build": cmd_build, "eval": cmd_eval, "tune": cmd_tune, "final": cmd_final, "report": cmd_report}[args.cmd](args)
