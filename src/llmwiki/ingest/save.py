"""Persist a ParsedDocument under raw/<space>/ with the karpathy-llm-wiki raw header."""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from llmwiki.config import Settings
from llmwiki.ingest.pii import mask_document
from llmwiki.models import ParsedDocument, RawRecord

_BAD_SEGMENT = re.compile(r'[\:*?"<>|\x00-\x1f]')
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_space(space: str) -> list[str]:
    if not space or not space.strip():
        raise ValueError("space is mandatory")
    segments = space.split("/")
    for seg in segments:
        if not seg.strip() or seg in (".", "..") or _BAD_SEGMENT.search(seg):
            raise ValueError(f"invalid space: {space!r}")
    return segments


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
    """Write raw/<space>/<YYYY-MM-DD-slug>.md; never overwrite (adds -2, -3, ...). Masks PII if enabled."""
    segments = _validate_space(space)
    if mask_pii if mask_pii is not None else settings.mask_pii_default:
        parsed, _ = mask_document(parsed)
    collected = (today or date.today()).isoformat()
    published = parsed.metadata.get("date", "")
    published = published if _DATE.match(published) else "Unknown"
    prefix = published if published != "Unknown" else collected
    base = f"{prefix}-{_slug(parsed.title or Path(parsed.source_name).stem)}"

    raw_root = settings.raw_dir.resolve()
    target_dir = raw_root.joinpath(*segments)
    if not target_dir.resolve().is_relative_to(raw_root):
        raise ValueError(f"space escapes raw dir: {space!r}")
    target_dir.mkdir(parents=True, exist_ok=True)

    content = (
        f"# {parsed.title}\n\n"
        f"> Source: {parsed.source_name}\n"
        f"> Collected: {collected}\n"
        f"> Published: {published}\n\n"
        f"{parsed.text}\n"
    ).encode("utf-8")

    n = 1
    while True:
        name = f"{base}.md" if n == 1 else f"{base}-{n}.md"
        path = target_dir / name
        try:
            with open(path, "xb") as f:  # exclusive create: never overwrite
                f.write(content)
            break
        except FileExistsError:
            n += 1
    return RawRecord(
        raw_path="/".join(["raw", *segments, name]),
        space="/".join(segments),
        sha256=hashlib.sha256(content).hexdigest(),
    )
