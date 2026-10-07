"""Admin CLI for wiki quality reports (read-only; never run automatically; the web layer cannot reach it).

  python -m llmwiki.lint_admin disputed    articles whose sources conflict (they carry a '## Disputed' section)

Prints one `<article path>\t<title>` line per article. This is an admin report: it is NOT filtered by space, so run it
only as the wiki administrator.
"""
from __future__ import annotations

import argparse
import sys

from .config import load_settings
from .engine.lint import lint_disputed
from .engine.store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmwiki.lint_admin", description="Wiki quality reports (admin only)")
    ap.add_argument("--db", help="SQLite path (default: <WIKI_DATA_DIR>/wiki.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("disputed", help="list articles with conflicting sources")
    args = ap.parse_args(argv)
    settings = load_settings()
    store = Store(args.db or settings.db_path)
    try:
        for p in lint_disputed(settings, store):
            print(f"{p}\t{store.article_title(p) or ''}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
