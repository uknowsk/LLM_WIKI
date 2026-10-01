"""CLI: python -m llmwiki.pipeline [--once] [--interval SECONDS]"""
from __future__ import annotations

import argparse
import sys

from llmwiki.audit import AuditLog
from llmwiki.config import load_settings
from llmwiki.engine.llm import OpenAICompatClient
from llmwiki.engine.store import Store
from llmwiki.pipeline.queue import JobQueue
from llmwiki.pipeline.run import process_file
from llmwiki.pipeline.watch import Watcher


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmwiki.pipeline", description="Turn files in inbox/<space>/ into wiki articles")
    ap.add_argument("--once", action="store_true", help="scan, process everything (with retries) and exit")
    ap.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds")
    args = ap.parse_args(argv)

    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store, audit, queue = Store(settings.db_path), AuditLog(settings.db_path), JobQueue(settings)
    llm = OpenAICompatClient.from_settings(settings)
    process = lambda path, space: process_file(path, space, settings, llm, store, audit)  # noqa: E731
    watcher = Watcher(settings, queue, audit, min_age=0.0 if args.once else 2.0)
    try:
        if args.once:
            counts = watcher.scan_once()
            results = queue.drain(process)
            final = {str(r.path): r.status for r in results}  # last attempt wins
            failed = [p for p, s in final.items() if s == "failed"]
            print(f"scan={counts} processed={len(final)} failed={len(failed)}")
            return 1 if failed else 0
        watcher.run_forever(process, args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        queue.close(), audit.close(), store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
