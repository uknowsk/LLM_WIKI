"""Admin CLI for the audit log (never run automatically; the web layer is read-only).

  python -m llmwiki.audit_admin prune --days N --yes     delete rows older than N days (N >= 30), records 'audit_prune'
  python -m llmwiki.audit_admin export --since ISO --out file.jsonl
Default policy: audit rows are kept forever (NO pruning) unless an admin runs prune.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import AuditLog
from .config import load_settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmwiki.audit_admin", description="Audit log retention / export (admin only)")
    ap.add_argument("--db", help="SQLite path (default: <WIKI_DATA_DIR>/wiki.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("prune", help="delete audit rows older than --days (minimum 30)")
    pr.add_argument("--days", type=int, required=True)
    pr.add_argument("--yes", action="store_true", help="really delete (without it only the count is shown)")
    ex = sub.add_parser("export", help="write rows since an ISO timestamp as JSON lines")
    ex.add_argument("--since", required=True, help="ISO 8601, naive = UTC, e.g. 2026-01-01T00:00:00")
    ex.add_argument("--out", required=True, help="output .jsonl (must not exist)")
    args = ap.parse_args(argv)
    audit = AuditLog(args.db or load_settings().db_path)
    try:
        if args.cmd == "prune":
            if args.days < AuditLog.MIN_PRUNE_DAYS:
                print(f"refused: --days must be >= {AuditLog.MIN_PRUNE_DAYS}", file=sys.stderr)
                return 2
            if not args.yes:
                print(f"dry run: {audit.count_older_than(args.days)} row(s) older than {args.days} days; add --yes to delete")
                return 2
            print(f"deleted {audit.prune(args.days)} row(s)")
            return 0
        try:
            rows = audit.entries_since(args.since)
        except ValueError:
            print("refused: --since is not a valid ISO 8601 timestamp", file=sys.stderr)
            return 2
        try:
            with open(Path(args.out), "x", encoding="utf-8", newline="\n") as f:
                for r in rows:
                    f.write(json.dumps(dict(zip(("ts", "user_id", "action", "target", "detail"), r)), ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"refused: cannot create output file ({type(exc).__name__})", file=sys.stderr)
            return 2
        audit.record(AuditLog.ADMIN_USER, "audit_export", Path(args.out).name[:200], f"rows={len(rows)} since={args.since[:40]}")
        print(f"exported {len(rows)} row(s)")
        return 0
    finally:
        audit.close()


if __name__ == "__main__":
    sys.exit(main())
