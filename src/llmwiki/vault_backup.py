"""Backup and restore of the personal second-brain vault (10_raw_data / 20_ai_wiki / 30_outputs).

  python -m llmwiki.vault_backup backup  [--vault .] [--dest DIR] [--keep 30]
  python -m llmwiki.vault_backup restore SNAPSHOT.zip --into EMPTY_DIR

backup: one zip per run (every file of the three folders, any type, dot-files included) named second-brain-YYYYMMDD-HHMMSS.zip
in the backup folder (--dest, else env WIKI_VAULT_BACKUP_DIR, else <vault parent>/second-brain-backup). A run whose content is
identical to the newest snapshot (compared by a sha256 digest kept in the zip comment) creates nothing. The zip is written to a
temp name, re-opened and verified, then renamed, so a half-written snapshot never looks complete. Only the newest --keep snapshots
stay. The backup folder holds the same plain-text notes as the vault: protect it like the vault. The destination may not be inside
one of the vault folders.

restore: unpacks into a folder that must be empty or missing (it never overwrites notes) and refuses paths that escape it.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from .vault_lint import OUT, RAW, WIKI

PREFIX = "second-brain-"
DEFAULT_KEEP = 30


class BackupError(Exception):
    pass


class VerifyError(BackupError):
    """The freshly written snapshot did not pass verification (nothing was kept)."""


@dataclass(frozen=True)
class BackupResult:
    status: str  # created | unchanged
    path: Path
    files: int
    pruned: int = 0


def _files(vault: Path) -> list[tuple[str, Path]]:
    out = []
    for folder in (RAW, WIKI, OUT):
        root = vault / folder
        if root.is_dir():
            out += [(p.relative_to(vault).as_posix(), p) for p in sorted(root.rglob("*")) if p.is_file()]
    return out


def _digest(files: list[tuple[str, Path]]) -> str:
    h = hashlib.sha256()
    for arc, p in sorted(files):
        h.update(arc.encode("utf-8") + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def _snapshots(dest: Path) -> list[Path]:
    return sorted(dest.glob(f"{PREFIX}*.zip")) if dest.is_dir() else []


def _comment(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            return z.comment.decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError):
        return ""


def backup(vault: Path, dest: Path, keep: int = DEFAULT_KEEP, now: datetime | None = None) -> BackupResult:
    vault, dest = Path(vault).resolve(), Path(dest).resolve()
    for folder in (RAW, WIKI):
        if not (vault / folder).is_dir():
            raise BackupError(f"missing folder: {folder} (is --vault the right folder?)")
    for folder in (RAW, WIKI, OUT):
        if dest == (vault / folder).resolve() or (vault / folder).resolve() in dest.parents:
            raise BackupError(f"the backup folder may not be inside the vault folder {folder}")
    files = _files(vault)
    digest = _digest(files)
    existing = _snapshots(dest)
    if existing and _comment(existing[-1]) == f"digest={digest}":
        return BackupResult("unchanged", existing[-1], len(files))
    dest.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    final, n = dest / f"{PREFIX}{stamp}.zip", 1
    while final.exists():  # two changed runs in the same second
        n += 1
        final = dest / f"{PREFIX}{stamp}-{n}.zip"
    tmp = dest / f".{final.name}.tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.comment = f"digest={digest}".encode("utf-8")
            for arc, p in files:
                z.write(p, arc)
        with zipfile.ZipFile(tmp) as z:
            if z.testzip() is not None or len(z.namelist()) != len(files):
                raise VerifyError("the new snapshot failed verification and was discarded")
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    snaps = _snapshots(dest)
    old = snaps[: max(0, len(snaps) - max(1, keep))]
    for p in old:
        p.unlink(missing_ok=True)
    return BackupResult("created", final, len(files), len(old))


def restore(snapshot: Path, into: Path) -> int:
    into = Path(into)
    if into.exists() and any(into.iterdir()):
        raise BackupError("the restore folder is not empty; restore into an empty or new folder (nothing is overwritten)")
    target_root = into.resolve()
    try:
        z = zipfile.ZipFile(snapshot)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError(f"cannot open the snapshot ({type(exc).__name__})") from None
    with z:
        members = [i for i in z.infolist() if not i.is_dir()]
        for i in members:  # validate everything before writing anything
            parts = PurePosixPath(i.filename).parts
            if PurePosixPath(i.filename).is_absolute() or ".." in parts or ":" in i.filename or not parts:
                raise BackupError("the snapshot contains a path outside the restore folder")
            if not (target_root / Path(*parts)).resolve().is_relative_to(target_root):
                raise BackupError("the snapshot contains a path outside the restore folder")
        for i in members:
            dest = target_root / Path(*PurePosixPath(i.filename).parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(i))
    return len(members)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmwiki.vault_backup", description="Second-brain vault backup / restore")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="write a snapshot zip into the backup folder")
    b.add_argument("--vault", default=".")
    b.add_argument("--dest", help="backup folder (default: env WIKI_VAULT_BACKUP_DIR, else <vault parent>/second-brain-backup)")
    b.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"snapshots to keep (default {DEFAULT_KEEP})")
    r = sub.add_parser("restore", help="unpack a snapshot into an empty folder")
    r.add_argument("snapshot")
    r.add_argument("--into", required=True)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    try:
        if args.cmd == "restore":
            print(f"restored {restore(Path(args.snapshot), Path(args.into))} file(s) into {args.into}")
            return 0
        vault = Path(args.vault)
        dest = Path(args.dest or os.environ.get("WIKI_VAULT_BACKUP_DIR") or vault.resolve().parent / "second-brain-backup")
        res = backup(vault, dest, keep=args.keep)
    except VerifyError as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 1
    except BackupError as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 2
    if res.status == "unchanged":
        print(f"unchanged: the newest snapshot already has this content ({res.path.name}, {res.files} files)")
    else:
        print(f"created {res.path} ({res.files} files, {res.pruned} old snapshot(s) removed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
