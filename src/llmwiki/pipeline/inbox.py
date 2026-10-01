"""Inbox layout: <data_dir>/inbox/<space>/<file>. The space is the relative folder path.

Fail closed: a file that is not inside a valid space folder is moved to inbox/_rejected and is
never processed (the pipeline never guesses a space). Folders starting with "_" are reserved.
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from llmwiki.config import Settings
from llmwiki.ingest.save import validate_space as _validate_space

RESERVED = ("_done", "_rejected", "_failed")
_TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".swp")


def inbox_dir(settings: Settings) -> Path:
    return settings.data_dir / "inbox"


class InvalidSpace(ValueError):
    pass


def validate_space(space: str) -> str:
    """Return the space or raise InvalidSpace. Uses the shared ingest validator plus the "_" reservation."""
    if isinstance(space, str) and any(seg.startswith("_") for seg in space.split("/")):
        raise InvalidSpace(f"reserved space segment: {space!r}")
    try:
        _validate_space(space)
    except ValueError as exc:
        raise InvalidSpace(f"invalid space: {space!r}") from exc
    return space


@dataclass(frozen=True)
class InboxItem:
    path: Path
    space: str | None  # None => outside any (valid) space folder => reject
    reason: str = ""


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    n = 2
    while True:
        cand = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        if not cand.exists():
            return cand
        n += 1


def is_temp_name(name: str) -> bool:
    return name.startswith((".", "~$")) or name.lower().endswith(_TEMP_SUFFIXES)


def scan(settings: Settings) -> list[InboxItem]:
    """List candidate files. Reserved folders and temp/partial files are skipped, symlinks rejected."""
    root = inbox_dir(settings)
    if not root.is_dir():
        return []
    items: list[InboxItem] = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if rel.parts[0] in RESERVED:
            continue
        if p.is_dir() or is_temp_name(p.name):
            continue
        if p.is_symlink() or not p.is_file():
            items.append(InboxItem(p, None, "not a regular file"))
            continue
        if len(rel.parts) < 2:
            items.append(InboxItem(p, None, "file is outside any space folder"))
            continue
        try:
            items.append(InboxItem(p, validate_space("/".join(rel.parts[:-1]))))
        except InvalidSpace as exc:
            items.append(InboxItem(p, None, str(exc)))
    return items


def _inside_inbox(settings: Settings, path: Path) -> Path | None:
    root = inbox_dir(settings).resolve()
    rp = path.resolve()
    if not rp.is_relative_to(root):
        return None
    rel = rp.relative_to(root)
    return rel if rel.parts and rel.parts[0] not in RESERVED else None


def _move(settings: Settings, path: Path, bucket: str, space: str | None, reason: str = "") -> Path | None:
    """Move an inbox file into inbox/<bucket>/<space>/. Files not in the live inbox are left alone."""
    rel = _inside_inbox(settings, path)
    if rel is None or not path.exists():
        return None
    dest_dir = inbox_dir(settings) / bucket
    if space:
        try:
            dest_dir = dest_dir.joinpath(*validate_space(space).split("/"))
        except InvalidSpace:
            pass  # unsafe space label: flatten into the bucket root
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique(dest_dir / path.name)
    shutil.move(str(path), str(dest))
    if reason:
        dest.with_name(dest.name + ".reason.txt").write_text(reason + "\n", encoding="utf-8")
    return dest


def mark_done(settings: Settings, path: Path, space: str) -> Path | None:
    return _move(settings, path, "_done", space)


def reject(settings: Settings, path: Path, space: str | None, reason: str) -> Path | None:
    return _move(settings, path, "_rejected", space, reason)


def mark_failed(settings: Settings, path: Path, space: str | None, reason: str) -> Path | None:
    return _move(settings, path, "_failed", space, reason)


def tombstone(settings: Settings, path: Path, space: str, note: str) -> Path | None:
    """Replace a processed inbox file by a small marker in _done (no copy of the original is kept)."""
    dest = mark_done(settings, path, space)
    if dest is None:
        return None
    sha = hashlib.sha256(dest.read_bytes()).hexdigest()
    dest.unlink()
    marker = dest.with_name(dest.name + ".processed.txt")
    marker.write_text(f"{note}\nsha256: {sha}\n", encoding="utf-8")
    return marker
