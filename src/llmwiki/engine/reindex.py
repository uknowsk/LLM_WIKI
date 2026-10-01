"""python -m llmwiki.engine.reindex: embed every article lacking an up-to-date vector (idempotent, resumable).

Each article is committed as soon as it is embedded, so an interrupted run simply continues next time.
Prints counts only, never article text.
"""
from __future__ import annotations

import argparse
import sys

from ..config import Settings, load_settings
from .embed import Embedder, embed_article, embedder_from_env
from .fsutil import read_text
from .store import Store


def reindex(settings: Settings, store: Store, embedder: Embedder) -> dict[str, int]:
    counts = {"embedded": 0, "skipped": 0, "failed": 0, "missing_file": 0}
    for path in store.article_paths():
        f = settings.wiki_dir / path
        if not f.is_file():
            counts["missing_file"] += 1
            continue
        res = embed_article(store, embedder, path, read_text(f))
        counts[{"ok": "embedded", "skipped": "skipped", "failed": "failed"}[res]] += 1
        if res == "failed":  # endpoint is down: stop instead of timing out once per article
            break
    return counts


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="llmwiki.engine.reindex", description=__doc__.splitlines()[0]).parse_args(argv)
    settings = load_settings()
    embedder = embedder_from_env(settings)
    if embedder is None:
        print("embeddings disabled (WIKI_EMBED_MODEL is off/empty)")
        return 2
    store = Store(settings.db_path)
    try:
        counts = reindex(settings, store, embedder)
    finally:
        store.close()
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
