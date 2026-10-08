"""CLI: python -m llmwiki.pipeline [--once] [--interval SECONDS]"""
from __future__ import annotations

import argparse
import sys

from llmwiki.audit import AuditLog
from llmwiki.config import load_settings
from llmwiki.engine.embed import embedder_from_env
from llmwiki.engine.llm import OpenAICompatClient
from llmwiki.engine.store import Store
from llmwiki.pipeline.aliases import AliasError, load_aliases
from llmwiki.pipeline.ocr_command import ocr_from_env
from llmwiki.pipeline.pii_policy import PiiPolicyError, load_pii_policy
from llmwiki.pipeline.queue import JobQueue
from llmwiki.pipeline.run import process_file
from llmwiki.pipeline.watch import Watcher


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmwiki.pipeline", description="Turn files in inbox/<space>/ into wiki articles")
    ap.add_argument("--once", action="store_true", help="scan, process everything (with retries) and exit")
    ap.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds")
    args = ap.parse_args(argv)

    settings = load_settings()
    try:  # fail closed: a bad policy/OCR config must stop startup, never silently disable masking
        mask_policy, ocr, aliases = load_pii_policy(), ocr_from_env(), load_aliases()
    except (PiiPolicyError, AliasError, ValueError) as exc:
        print(f"startup refused: {exc}", file=sys.stderr)
        return 2
    extractor = None
    try:
        import pypdf  # noqa: F401
        from llmwiki.ingest.pdf import PypdfExtractor
        extractor = PypdfExtractor()
    except ImportError:
        pass  # PDFs then fail per file with "pypdf is not installed" (install the [pdf] extra)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store, audit, queue = Store(settings.db_path), AuditLog(settings.db_path), JobQueue(settings)
    llm = OpenAICompatClient.from_settings(settings)
    embedder = embedder_from_env(settings)  # None when WIKI_EMBED_MODEL=off
    process = lambda path, space: process_file(  # noqa: E731
        path, space, settings, llm, store, audit, embedder=embedder, ocr=ocr, extractor=extractor, mask_policy=mask_policy, aliases=aliases,
        verify_folder=True)
    queue.drop_pending()  # pending jobs are re-derived by the next scan (folder/alias may have changed)
    watcher = Watcher(settings, queue, audit, min_age=0.0 if args.once else 2.0, aliases=aliases)
    try:
        if args.once:
            counts = watcher.scan_once()
            results = queue.drain(process)
            final = {str(r.path): r.status for r in results}  # last attempt wins
            failed = [p for p, s in final.items() if s == "failed"]
            print(f"scan={counts} deferred={watcher.deferred} processed={len(final)} failed={len(failed)}")
            if watcher.deferred:
                print(f"{watcher.deferred} file(s) were left in the inbox because the queue is full; run again to process them")
            return 1 if failed else 0
        watcher.run_forever(process, args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        queue.close(), audit.close(), store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
