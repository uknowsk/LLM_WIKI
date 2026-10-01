"""Persist a ParsedDocument under raw/<space>/ with the karpathy-llm-wiki raw header."""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from llmwiki.config import Settings
from llmwiki.ingest.pii import mask_document
from llmwiki.models import ParsedDocument, RawRecord

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_MAX_SEGMENTS = 4
_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})


def validate_space(space: str) -> list[str]:
    """Single shared space-name validator. Returns the path segments or raises ValueError.

    Each segment: lowercase [a-z0-9][a-z0-9-]{0,62}, not a Windows reserved device name; max 4 segments.
    """
    if not isinstance(space, str) or not space.strip():
        raise ValueError("space is mandatory")
    segments = space.split("/")
    if len(segments) > _MAX_SEGMENTS:
        raise ValueError(f"invalid space (more than {_MAX_SEGMENTS} segments): {space!r}")
    for seg in segments:
        if not _SEGMENT.fullmatch(seg) or seg in _RESERVED:
            raise ValueError(f"invalid space: {space!r}")
    return segments


def _one_line(value: str) -> str:
    return " ".join(str(value).split())


def _slug(text: str) -> str:
    s = re.sub(r"[^\w]+", "-", text, flags=re.UNICODE).strip("-_").lower()
    return s[:60].strip("-_") or "untitled"


def save_raw(
    parsed: ParsedDocument,
    space: str,
    settings: Settings,
    *,
    mask_pii: bool | None = None,
    today: date | None = None,
) -> RawRecord:
    """Write raw/<space>/<YYYY-MM-DD-slug>.md; never overwrite (adds -2, -3, ... for different content;
    identical content returns the existing record). Masks PII if enabled."""
    segments = validate_space(space)
    if mask_pii if mask_pii is not None else settings.mask_pii_default:
        parsed, _ = mask_document(parsed)
    title, source = _one_line(parsed.title), _one_line(parsed.source_name)
    collected = (today or date.today()).isoformat()
    published = parsed.metadata.get("date", "")
    published = published if _DATE.fullmatch(published) else "Unknown"
    prefix = published if published != "Unknown" else collected
    base = f"{prefix}-{_slug(title or Path(source).stem)}"

    raw_root = settings.raw_dir.resolve()
    target_dir = raw_root.joinpath(*segments)
    if not target_dir.resolve().is_relative_to(raw_root):
        raise ValueError(f"space escapes raw dir: {space!r}")
    target_dir.mkdir(parents=True, exist_ok=True)

    content = (
        f"# {title}\n\n"
        f"> Source: {source}\n"
        f"> Collected: {collected}\n"
        f"> Published: {published}\n\n"
        f"{parsed.text}\n"
    ).encode("utf-8")

    sha = hashlib.sha256(content).hexdigest()
    n = 1
    while True:
        name = f"{base}.md" if n == 1 else f"{base}-{n}.md"
        path = target_dir / name
        try:
            with open(path, "xb") as f:  # exclusive create: never overwrite
                f.write(content)
            break
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() == sha:  # identical content already stored
                break
            n += 1
    return RawRecord(
        raw_path="/".join(["raw", *segments, name]),
        space="/".join(segments),
        sha256=sha,
    )
