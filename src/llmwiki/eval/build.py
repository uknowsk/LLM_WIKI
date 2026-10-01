"""`build`: ingest a corpus through the real pipeline into a reusable data-dir snapshot + gold_map.json."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from ..audit import AuditLog
from ..config import Settings
from ..engine.embed import Embedder, embed_article
from ..engine.fsutil import read_text
from ..engine.llm import LLMClient
from ..engine.store import Store
from ..pipeline.run import SUPPORTED, process_file

ABORT_AFTER = 5  # consecutive failures => the LLM endpoint is most likely down: stop, resume later


def corpus_files(corpus: Path) -> list[tuple[str, Path, str]]:
    """[(doc_id, path, space)] sorted; space = first directory under the corpus root."""
    out = []
    for p in sorted(corpus.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED or p.name.startswith((".", "~$")):
            continue
        rel = p.relative_to(corpus).as_posix()
        if "/" not in rel:
            continue  # files directly under the corpus root have no space
        out.append((rel, p, rel.split("/", 1)[0]))
    return out


def write_gold_map(settings: Settings, files: list[tuple[str, Path, str]]) -> dict[str, dict]:
    """corpus file -> raw paths (+ attachments of a mail) -> articles, read back from the snapshot's DB."""
    db = sqlite3.connect(str(settings.db_path))
    try:
        gold: dict[str, dict] = {}
        for doc, p, space in files:
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            row = db.execute("SELECT raw_path FROM pipeline_sources WHERE space = ? AND sha256 = ?", (space, sha)).fetchone()
            raws: list[str] = []
            if row:
                raws = [row[0]] + [r[0] for r in db.execute(
                    "SELECT raw_path FROM pipeline_sources WHERE space = ? AND parent_raw = ?", (space, row[0]))]
            arts = sorted({r[0] for raw in raws for r in db.execute(
                "SELECT article_path FROM article_sources WHERE raw_path = ?", (raw,))})
            gold[doc] = {"space": space, "raw_paths": raws, "articles": arts}
    finally:
        db.close()
    (settings.data_dir / "gold_map.json").write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
    return gold


def build(corpus: Path | str, settings: Settings, llm: LLMClient, embedder: Embedder | None,
          log: Callable[[str], None] | None = None) -> dict:
    corpus = Path(corpus)
    files = corpus_files(corpus)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store, audit = Store(settings.db_path), AuditLog(settings.db_path)
    counts = {"files": len(files), "done": 0, "skipped": 0, "rejected": 0, "failed": 0, "aborted": 0}
    failures: list[dict] = []
    streak = 0
    try:
        for i, (doc, p, space) in enumerate(files, 1):
            res = process_file(p, space, settings, llm, store, audit, embedder=embedder)
            counts[res.status] = counts.get(res.status, 0) + 1
            if res.status in ("failed", "rejected"):
                failures.append({"doc": doc, "status": res.status, "error": res.error})
            streak = streak + 1 if res.status == "failed" else 0
            if log:
                log(f"[{i}/{len(files)}] {res.status}")
            if streak >= ABORT_AFTER:
                counts["aborted"] = 1
                break
        if embedder is not None:  # backfill vectors that failed during a past (interrupted) run; no-op otherwise
            for art in store.article_paths():
                f = settings.wiki_dir / art
                if f.is_file():
                    embed_article(store, embedder, art, read_text(f))
        gold = write_gold_map(settings, files)
        counts["articles"] = len(store.article_paths())
        counts["embedded"] = len(store.embedding_meta())
        counts["docs_without_article"] = sum(1 for g in gold.values() if not g["articles"])
    finally:
        store.close()
        audit.close()
    (settings.data_dir / "build_report.json").write_text(
        json.dumps({"counts": counts, "failures": failures}, ensure_ascii=False, indent=1), encoding="utf-8")
    return counts
