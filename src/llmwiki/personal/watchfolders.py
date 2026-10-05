"""Watch folders: documents that already live on this PC. A new/changed supported file is COPIED into the staging
folder (<home>/inbox/watch/<sha16>/) and the copy is processed. The original is only ever opened for reading: it is
never moved, renamed, modified or deleted (also not when PII masking is on). Vanished originals change nothing."""
from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import unicodedata
from pathlib import Path

from ..ingest.limits import max_input_bytes
from ..pipeline import inbox
from ..pipeline.run import SUPPORTED

STAGE_NAME = "watch"
UNSTAGEABLE = "skipped_unstageable"  # sha256 column marker: cannot be staged; retried only when size/mtime change
MAX_STAGED_PATH = 240  # leave headroom below MAX_PATH (260) for the moves into _done/_failed
_REPARSE, _HIDDEN_SYSTEM = inbox.FILE_ATTRIBUTE_REPARSE_POINT, inbox.FILE_ATTRIBUTE_HIDDEN | inbox.FILE_ATTRIBUTE_SYSTEM
_BAD_CHARS = re.compile(r'[<>:"/\|?*\x00-\x1f]')
_CHUNK = 1024 * 1024
_SKIP_DIRS = frozenset({"$recycle.bin", "system volume information", "node_modules"})
_SCHEMA = """CREATE TABLE IF NOT EXISTS watched (
    path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL, seen_at REAL NOT NULL)"""


def _attrs(entry) -> int:
    try:
        return getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return 0


def _is_link(entry) -> bool:
    try:
        if entry.is_symlink() or (getattr(entry, "is_junction", None) and entry.is_junction()):
            return True
    except OSError:
        return True
    return bool(_attrs(entry) & _REPARSE)


def _reparse(st) -> bool:
    return bool((getattr(st, "st_file_attributes", 0) or 0) & _REPARSE)


def _same_file(a, b) -> bool:
    """Same size/mtime and, where the OS reports them (non-zero), the same device and file id."""
    if (a.st_size, a.st_mtime_ns) != (b.st_size, b.st_mtime_ns):
        return False
    return not (a.st_ino and b.st_ino and (a.st_ino, a.st_dev) != (b.st_ino, b.st_dev))


_DEVICES = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})


def _safe_name(name: str) -> str:
    """File name for the staged copy. Raises ValueError for Windows device names (also with extensions, trailing
    dots or spaces), which would open a device instead of a file."""
    out = _BAD_CHARS.sub("_", name).rstrip(". ") or "file"
    if any(unicodedata.normalize("NFKC", seg).lower().strip(" .") in _DEVICES for seg in out.split(".")):
        raise ValueError("reserved device name")
    return out


class WatchIndex:
    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def get(self, key: str) -> tuple[int, int, str] | None:
        with self._lock:
            return self._db.execute("SELECT size, mtime_ns, sha256 FROM watched WHERE path = ?", (key,)).fetchone()

    def put(self, key: str, size: int, mtime_ns: int, sha: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO watched (path, size, mtime_ns, sha256, seen_at) VALUES (?,?,?,?,?)",
                             (key, size, mtime_ns, sha, time.time()))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


class WatchScanner:
    def __init__(self, home: Path, roots: list[Path], index: WatchIndex, *, min_age: float = 3.0,
                 max_pending: int = 50, max_bytes: int | None = None):
        self.home = Path(os.path.realpath(home))
        self.stage = Path(home) / "inbox" / STAGE_NAME
        self.roots, self.index, self.min_age, self.max_pending = list(roots), index, min_age, max_pending
        self.max_bytes = max_bytes if max_bytes is not None else max_input_bytes()
        self._home_key = os.path.normcase(str(self.home))

    def _inside_home(self, path: str) -> bool:
        p = os.path.normcase(path)
        return p == self._home_key or p.startswith(self._home_key + os.sep)

    def usable_roots(self) -> list[Path]:
        """Roots that exist, are real folders and are not inside the data home (no feedback loop)."""
        out = []
        for r in self.roots:
            try:
                real = Path(os.path.realpath(r))
                if real.is_dir() and not self._inside_home(str(real)):
                    out.append(real)
            except OSError:
                continue
        return out

    def candidates(self, root: Path):
        """Yield DirEntry of supported, plausible document files below root. Never descends into links, hidden/system
        folders, housekeeping folders or the data home."""
        stack = [str(root)]
        while stack:
            try:
                with os.scandir(stack.pop()) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                if _attrs(e) & _HIDDEN_SYSTEM or _is_link(e) or self._inside_home(e.path):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not e.name.startswith(".") and e.name.casefold() not in _SKIP_DIRS:
                            stack.append(e.path)
                    elif (e.is_file(follow_symlinks=False) and not inbox.is_temp_name(e.name)
                          and Path(e.name).suffix.lower() in SUPPORTED):
                        yield e
                except OSError:
                    continue

    def _pending(self) -> int:
        n = 0
        for _d, _dirs, files in os.walk(self.stage):
            n += sum(1 for f in files if not f.startswith("."))
        return n

    def scan(self) -> dict[str, int]:
        counts = {"staged": 0, "unchanged": 0, "waiting": 0, "skipped": 0}
        pending = self._pending()
        for root in self.usable_roots():
            for e in self.candidates(root):
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_size == 0 or st.st_size > self.max_bytes:
                    counts["skipped"] += 1
                    continue
                row = self.index.get(e.path)
                if row and row[:2] == (st.st_size, st.st_mtime_ns):
                    counts["unchanged"] += 1
                    continue
                if 0 <= time.time() - st.st_mtime < self.min_age or pending >= self.max_pending:  # future mtimes are not "fresh"
                    counts["waiting"] += 1  # still being saved, or the staging queue is full: next scan
                    continue
                if self._stage(e.path, e.name, st, row):
                    counts["staged"] += 1
                    pending += 1
                else:
                    counts["waiting"] += 1
        return counts

    def _unstageable(self, src: str, st) -> bool:
        self.index.put(src, st.st_size, st.st_mtime_ns, UNSTAGEABLE)  # not retried until size/mtime change
        return False

    def _stage(self, src: str, name: str, st, row) -> bool:
        """Copy (read-only on src) -> verify it did not change meanwhile -> publish atomically. False = not staged
        (retried next scan, or recorded as unstageable and retried only when size/mtime change)."""
        try:
            safe = _safe_name(name)
        except ValueError:  # Windows device name (nul, con, ...)
            return self._unstageable(src, st)
        if len(str(self.stage)) + 1 + 16 + 1 + len(safe) > MAX_STAGED_PATH:
            return self._unstageable(src, st)  # would exceed MAX_PATH
        self.stage.mkdir(parents=True, exist_ok=True)
        tmp = self.stage / f".copy-{secrets.token_hex(8)}.part"  # dot-prefixed: never picked up as a document
        sha = hashlib.sha256()
        try:
            before = os.lstat(src)
            if _reparse(before) or stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                return False  # swapped for a link since the scan: never follow
            with open(src, "rb") as fin, open(tmp, "xb") as out:
                # the handle must be the very file we looked at (a swap between lstat and open changes these)
                if not _same_file(before, os.fstat(fin.fileno())):
                    return False
                while chunk := fin.read(_CHUNK):
                    sha.update(chunk)
                    out.write(chunk)
                if not _same_file(before, os.fstat(fin.fileno())):
                    return False
            after = os.lstat(src)
            if _reparse(after) or stat.S_ISLNK(after.st_mode) or not _same_file(before, after)                     or (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                return False  # changed or swapped while copying: discard the temp copy
            digest = sha.hexdigest()
            if row and row[2] == digest:  # touched but identical content
                self.index.put(src, st.st_size, st.st_mtime_ns, digest)
                return False
            dest_dir = self.stage / digest[:16]
            dest_dir.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dest_dir / safe)
            self.index.put(src, st.st_size, st.st_mtime_ns, digest)
            return True
        except OSError as exc:
            if isinstance(exc, FileNotFoundError) and not os.path.exists(src):
                return False  # vanished
            if getattr(exc, "winerror", None) in (3, 123, 206) or exc.errno == errno.ENAMETOOLONG:
                return self._unstageable(src, st)  # path/name problem on our side: not worth retrying every scan
            return False  # locked by another program, disk full: try again next scan
        finally:
            tmp.unlink(missing_ok=True)
