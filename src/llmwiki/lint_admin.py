"""Admin CLI for wiki quality reports (read-only; never run automatically; the web layer cannot reach it).

  python -m llmwiki.lint_admin disputed         articles whose sources conflict (they carry a '## Disputed' section)
  python -m llmwiki.lint_admin feedback         answer feedback per article: <down> <up> <path> <title>, most 'down' first
  python -m llmwiki.lint_admin gaps [--limit N] questions that ended in "no evidence": <count> <question>, most frequent first
  python -m llmwiki.lint_admin stats            usage counters from the audit log (queries, no_evidence, users, feedback)

Output is tab separated. These are admin reports: they are NOT filtered by space (the gaps report shows the users'
own question text, as the audit log does), so run them only as the wiki administrator.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from .audit import AuditLog
from .config import load_settings
from .engine.lint import lint_disputed
from .engine.store import Store

DEFAULT_GAP_LIMIT = 50


def feedback_report(rows: list[tuple]) -> list[tuple[int, int, str]]:
    tally: dict[str, list[int]] = {}
    for _, _, action, target, _ in rows:
        if action in ("feedback_up", "feedback_down") and target != "-":
            tally.setdefault(target, [0, 0])[0 if action == "feedback_down" else 1] += 1
    return [(d, u, p) for p, (d, u) in sorted(tally.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0]))]


def gaps_report(rows: list[tuple], limit: int) -> list[tuple[int, str]]:
    asked = Counter(t.strip() for _, _, action, t, detail in rows if action == "query" and detail == "no-evidence" and t.strip())
    return [(n, q) for q, n in sorted(asked.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def stats_report(rows: list[tuple]) -> dict[str, int]:
    queries = [r for r in rows if r[2] == "query"]
    return {"queries": len(queries),
            "no_evidence": sum(1 for r in queries if r[4] == "no-evidence"),
            "users": len({r[1] for r in queries}),
            "feedback_up": sum(1 for r in rows if r[2] == "feedback_up"),
            "feedback_down": sum(1 for r in rows if r[2] == "feedback_down")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmwiki.lint_admin", description="Wiki quality reports (admin only)")
    ap.add_argument("--db", help="SQLite path (default: <WIKI_DATA_DIR>/wiki.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("disputed", help="list articles with conflicting sources")
    sub.add_parser("feedback", help="answer feedback per article: <down>\\t<up>\\t<path>\\t<title>, most 'down' first")
    gp = sub.add_parser("gaps", help="questions that ended in 'no evidence', most frequent first")
    gp.add_argument("--limit", type=int, default=DEFAULT_GAP_LIMIT)
    sub.add_parser("stats", help="usage counters from the audit log")
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
        if args.cmd == "feedback":
            for down, up, p in feedback_report(rows):
                print(f"{down}\t{up}\t{p}\t{store.article_title(p) or ''}")
        elif args.cmd == "gaps":
            for n, q in gaps_report(rows, max(1, args.limit)):
                print(f"{n}\t{q}")
        else:
            for k, v in stats_report(rows).items():
                print(f"{k}\t{v}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
