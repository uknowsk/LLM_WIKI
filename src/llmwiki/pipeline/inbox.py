"""Inbox layout: <data_dir>/inbox/<space>/<file>. The space is derived ONLY from the folder path.

Fail closed: a file that is not inside a valid space folder (canonical name or WIKI_INBOX_ALIASES alias) is moved to
inbox/_rejected and is never processed (the pipeline never guesses a space). Folders starting with "_" are reserved.
On a shared network folder Windows ACLs decide who may write where; we never look at file owner or content.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from llmwiki.config import Settings
from llmwiki.ingest.save import validate_space as _validate_space
from llmwiki.pipeline import notes

if TYPE_CHECKING:
    from llmwiki.pipeline.aliases import AliasMap

RESERVED = ("_done", "_rejected", "_failed")
MAX_DEPTH = 6  # folder levels below the inbox that are walked; deeper folders are reported, never entered
MAX_NAME = 200  # longer file names are rejected at scan time (name + our suffixes must fit NTFS's 255)
MAX_DEST_NAME = 220  # file names inside _done/_rejected/_failed are truncated to this (+ .reason.txt etc. <= 240)
log = logging.getLogger("llmwiki.pipeline")
_TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".swp", ".lnk")
_HOUSEKEEPING = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})
_HOUSEKEEPING_DIRS = frozenset({"$recycle.bin", "system volume information"})
FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM, FILE_ATTRIBUTE_REPARSE_POINT = 0x2, 0x4, 0x400
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DEVICES = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})


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
    is_dir: bool = False  # a linked folder: reported, never moved or followed
    category: str = ""  # notes.* category for the worker feedback note


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
    """Housekeeping that appears on shared folders: Office lock files, thumbnails, shortcuts, partial copies, our notes."""
    low = unicodedata.normalize("NFC", name).lower()
    return (low.startswith((".", "~$")) or low.endswith(_TEMP_SUFFIXES) or low in _HOUSEKEEPING
            or notes.is_note_name(name))


def is_sharing_violation(exc: BaseException) -> bool:
    """Windows ERROR_SHARING_VIOLATION (32) / ERROR_LOCK_VIOLATION (33): another process holds the file, transient.
    A plain access denial (winerror 5 / EACCES) is NOT transient and must consume attempts."""
    return getattr(exc, "winerror", None) in (32, 33)


def bad_filename(name: str) -> bool:
    """Names Windows would not create itself (non-Windows clients on the share): path tricks, ADS, devices, trailing dot."""
    return (bool(_BAD_NAME_CHARS.search(name)) or name.endswith((".", " ")) or ".." in name or len(name) > MAX_NAME
            or name.split(".")[0].strip().casefold() in _DEVICES)


def is_reserved(part: str) -> bool:
    """Root-level folders starting with "_" are ours (_done, _rejected, _failed, ...). NTFS is case-insensitive, so
    `_Done` is `_done`: the rule is on the prefix, not on exact names."""
    return part.startswith("_")


def _safe_dest_name(name: str) -> str:
    name = _BAD_NAME_CHARS.sub("_", name).rstrip(". ") or "file"
    if len(name) > MAX_DEST_NAME:
        stem, ext = os.path.splitext(name)
        ext = ext[:16]
        name = stem[: MAX_DEST_NAME - len(ext)] + ext
    return name


def _attrs(obj) -> int:
    """Windows file attributes of a DirEntry/Path without following links (0 elsewhere). Test seam."""
    try:
        return getattr(obj.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return 0


def _is_link(obj) -> bool:
    """Symlink, NTFS junction or any other reparse point: could point into another department's folder."""
    try:
        if obj.is_symlink():
            return True
        junction = getattr(obj, "is_junction", None)
        if junction is not None and junction():
            return True
    except OSError:
        return True
    return bool(_attrs(obj) & FILE_ATTRIBUTE_REPARSE_POINT)


def has_link_component(settings: Settings, path: Path) -> bool:
    """True if the path or any folder between the inbox root and it is a link/reparse point."""
    root = inbox_dir(settings)
    try:
        rel = Path(path).relative_to(root)
    except ValueError:
        return False
    cur = root
    for part in rel.parts:
        cur = cur / part
        if _is_link(cur):
            return True
    return False


def _resolve_space(folder: tuple[str, ...], aliases: AliasMap | None) -> str:
    if aliases is not None:
        hit = aliases.resolve(folder)
        if hit is not None:
            return hit
    return validate_space("/".join(folder))


def _scan_dir(directory: Path, rel: tuple[str, ...], aliases: AliasMap | None, out: list[InboxItem]) -> None:
    try:
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return
    for e in entries:
        name, parts = e.name, rel + (e.name,)
        if not rel and (name.casefold() in RESERVED or (is_reserved(name) and e.is_dir(follow_symlinks=False))):
            continue
        if _attrs(e) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
            continue  # hidden/system entries (recycle bin, Explorer leftovers) are never user documents
        real_dir = e.is_dir(follow_symlinks=False)
        if not real_dir and is_temp_name(name):
            continue
        path = Path(e.path)
        if _is_link(e):
            try:
                linked_dir = e.is_dir()
            except OSError:
                linked_dir = False
            out.append(InboxItem(path, None, "linked folder/file (symlink or reparse point)", linked_dir, notes.LINKED))
        elif real_dir:
            if len(parts) > MAX_DEPTH:  # no recursion without a bound (a user can nest thousands of folders)
                out.append(InboxItem(path, None, "folder nested too deeply", True, notes.LOCATION))
            elif name.casefold() not in _HOUSEKEEPING_DIRS:
                _scan_dir(path, parts, aliases, out)
        elif not e.is_file(follow_symlinks=False):
            out.append(InboxItem(path, None, "not a regular file", False, notes.FAILED))
        elif bad_filename(name):
            out.append(InboxItem(path, None, "invalid file name", False, notes.BAD_NAME))
        elif len(parts) < 2:
            out.append(InboxItem(path, None, "file is outside any space folder", False, notes.LOCATION))
        else:
            try:
                out.append(InboxItem(path, _resolve_space(parts[:-1], aliases)))
            except InvalidSpace as exc:
                out.append(InboxItem(path, None, str(exc), False, notes.LOCATION))


def scan(settings: Settings, aliases: AliasMap | None = None) -> list[InboxItem]:
    """List candidate files. Reserved folders, hidden/system and housekeeping files are skipped silently;
    links (symlink/junction) are reported and never followed."""
    root = inbox_dir(settings)
    if not root.is_dir():
        return []
    items: list[InboxItem] = []
    _scan_dir(root, (), aliases, items)
    return items


def _inside_inbox(settings: Settings, path: Path) -> Path | None:
    root = inbox_dir(settings).resolve()
    rp = path.resolve()
    if not rp.is_relative_to(root):
        return None
    rel = rp.relative_to(root)
    return rel if rel.parts and not is_reserved(rel.parts[0]) else None


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
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique(dest_dir / _safe_dest_name(path.name))
        shutil.move(str(path), str(dest))
    except OSError as exc:  # locked, no permission, share gone: the file simply stays where it is
        log.warning("move to %s failed (%s)", bucket, type(exc).__name__)
        return None
    if reason:
        try:
            dest.with_name(dest.name + ".reason.txt").write_text(reason + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("reason file not written (%s)", type(exc).__name__)
    return dest


def mark_done(settings: Settings, path: Path, space: str) -> Path | None:
    return _move(settings, path, "_done", space)


def reject(settings: Settings, path: Path, space: str | None, reason: str) -> Path | None:
    return _move(settings, path, "_rejected", space, reason)


def mark_failed(settings: Settings, path: Path, space: str | None, reason: str) -> Path | None:
    return _move(settings, path, "_failed", space, reason)


def tombstone(settings: Settings, path: Path, space: str, note: str) -> Path | None:
    """Replace a processed inbox file by a small marker in _done (no copy of the original is kept).
    Markers are created exclusively (-2, -3 ...): an earlier marker's sha256 line is never overwritten."""
    dest = mark_done(settings, path, space)
    if dest is None:
        return None
    try:
        sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        dest.unlink()
        for n in range(1, 1000):
            marker = dest.with_name(f"{dest.name}.processed.txt" if n == 1 else f"{dest.name}.processed-{n}.txt")
            try:
                with open(marker, "x", encoding="utf-8") as f:
                    f.write(f"{note}\nsha256: {sha}\n")
                return marker
            except FileExistsError:
                continue
    except OSError as exc:
        log.warning("tombstone failed (%s)", type(exc).__name__)
    return None


def space_for_path(settings: Settings, path: Path, aliases: AliasMap | None = None) -> str | None:
    """Re-derive the space from the folder with the SAME rule the scanner uses (None: path is not in the live inbox).
    Raises InvalidSpace when the folder is not a valid/aliased space."""
    try:
        rel = Path(path).relative_to(inbox_dir(settings))
    except ValueError:
        return None
    if len(rel.parts) < 2 or is_reserved(rel.parts[0]):
        raise InvalidSpace("file is outside any space folder")
    return _resolve_space(rel.parts[:-1], aliases)


def _notice_folder(settings: Settings, path: Path) -> Path | None:
    """The user-facing folder a note may go to: inside a (non-reserved, non-root, non-linked) inbox folder only.
    Root-level files get no note: everybody could see the file name there."""
    folder, root = Path(path).parent, inbox_dir(settings)
    try:
        rel = folder.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if not rel.parts or is_reserved(rel.parts[0]) or has_link_component(settings, folder):
        return None
    return folder


def notify(settings: Settings, path: Path, category: str) -> Path | None:
    folder = _notice_folder(settings, path)
    return notes.write_note(folder, Path(path).name, category) if folder is not None else None


def clear_notice(settings: Settings, path: Path) -> None:
    folder = _notice_folder(settings, path)
    if folder is not None:
        notes.clear_notes(folder, Path(path).name)
