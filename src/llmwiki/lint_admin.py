"""Admin CLI for wiki quality reports (read-only; never run automatically; the web layer cannot reach it).

  python -m llmwiki.lint_admin disputed    articles whose sources conflict (they carry a '## Disputed' section)
  python -m llmwiki.lint_admin feedback    answer feedback per article (users' up/down votes), most 'down' first

Prints one `<article path>\t<title>` line per article. This is an admin report: it is NOT filtered by space, so run it
only as the wiki administrator.
"""
from __future__ import annotations

import argparse
import sys

from .audit import AuditLog
from .config import load_settings
from .engine.lint import lint_disputed
from .engine.store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmwiki.lint_admin", description="Wiki quality reports (admin only)")
    ap.add_argument("--db", help="SQLite path (default: <WIKI_DATA_DIR>/wiki.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("disputed", help="list articles with conflicting sources")
    sub.add_parser("feedback", help="answer feedback per article: <down>\\t<up>\\t<path>\\t<title>, most 'down' first")
    args = ap.parse_args(argv)
    settings = load_settings()
    store = Store(args.db or settings.db_path)
    try:
        if args.cmd == "disputed":
            for p in lint_disputed(settings, store):
                print(f"{p}\t{store.article_title(p) or ''}")
            return 0
        audit = AuditLog(args.db or settings.db_path)
        try:
            rows = audit.entries()
        finally:
            audit.close()
        tally: dict[str, list[int]] = {}
        for _, _, action, target, _ in rows:
            if action in ("feedback_up", "feedback_down") and target != "-":
                tally.setdefault(target, [0, 0])[0 if action == "feedback_down" else 1] += 1
        for p, (down, up) in sorted(tally.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0])):
            print(f"{down}\t{up}\t{p}\t{store.article_title(p) or ''}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
