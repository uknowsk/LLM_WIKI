"""`final`: evaluate ONE chosen config on the held-out split; the guard prevents tuning on it."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .runner import Harness
from .scoring import summarize, type_scores
from .stages import make_params, params_dict
from .tuner import config_hash

GUARD = "heldout_used.json"


class HeldoutGuardError(RuntimeError):
    pass


def load_config(path_or_dict: Path | str | dict) -> dict:
    """Accepts a bare params dict, a trial record ({"params": ...}) or a best-*.json file."""
    d = path_or_dict if isinstance(path_or_dict, dict) else json.loads(Path(path_or_dict).read_text(encoding="utf-8"))
    return dict(d.get("params", d))


def params_hash(params: dict) -> str:
    """Hash of the FULL normalized RetrievalParams: configs that behave identically hash identically."""
    return config_hash(params_dict(make_params(params)))


def _guard_path(results_dir: Path) -> Path:
    return results_dir / GUARD


def check_guard(results_dir: Path | str, h: str, force: bool) -> None:
    p = _guard_path(Path(results_dir))
    used = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    if h in used and not force:
        raise HeldoutGuardError(
            f"config {h} was already evaluated on the held-out split ({used[h]['ts']}). Tuning on the held-out "
            "set invalidates it; use --force only for a deliberate re-check.")


def _mark_used(results_dir: Path, h: str, params: dict) -> None:
    p = _guard_path(results_dir)
    used = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    used[h] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "params": params}
    p.write_text(json.dumps(used, ensure_ascii=False, indent=1), encoding="utf-8")


def gap_table(tune_rows: list[dict], held_rows: list[dict], mode: str) -> dict:
    """Per type: tune score, heldout score, gap = tune - heldout (positive = overfit). 'overall' = objective."""
    a, b = type_scores(tune_rows, mode), type_scores(held_rows, mode)
    out = {t: {"tune": a.get(t), "heldout": b.get(t),
               "gap": (a[t] - b[t]) if t in a and t in b else None} for t in sorted(set(a) | set(b))}
    sa, sb = summarize(tune_rows, mode)["objective"], summarize(held_rows, mode)["objective"]
    out["overall"] = {"tune": sa, "heldout": sb, "gap": sa - sb}
    return out


def run_final(h: Harness, params_in: dict, results_dir: Path | str, modes: tuple[str, ...] = ("retrieval", "answer"),
              force: bool = False) -> dict:
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    p = make_params(params_in)
    ph = params_hash(params_in)
    check_guard(results_dir, ph, force)
    tune_q = [q for q in h.qa if q.split == "tune"]
    held_q = [q for q in h.qa if q.split == "heldout"]
    if not held_q:
        raise ValueError("the dataset has no heldout questions")
    report: dict = {"config_hash": ph, "params": params_dict(p), "n_tune": len(tune_q), "n_heldout": len(held_q), "modes": {}}
    for mode in modes:
        tr, hr = h.run(p, mode, tune_q), h.run(p, mode, held_q)
        report["modes"][mode] = {"tune": summarize(tr, mode), "heldout": summarize(hr, mode),
                                 "gap": gap_table(tr, hr, mode), "heldout_rows": hr}
    _mark_used(results_dir, ph, params_dict(p))
    (results_dir / f"final-{ph}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report
