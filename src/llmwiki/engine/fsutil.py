"""Small file helpers: atomic LF writes and transient-lock-tolerant reads (Windows replace/open races)."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_RETRIES = 100
_DELAY = 0.01


def atomic_write(path: Path, text: str) -> None:
    """Write `text` (UTF-8, LF newlines) to a temp file in the same directory, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    try:
        for i in range(_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # a reader holds the target open (Windows)
                if i == _RETRIES - 1:
                    raise
                time.sleep(_DELAY)
    finally:
        tmp.unlink(missing_ok=True)


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


def read_text(path: Path) -> str:
    for i in range(_RETRIES):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except PermissionError:
            if i == _RETRIES - 1:
                raise
            time.sleep(_DELAY)
    raise AssertionError("unreachable")
