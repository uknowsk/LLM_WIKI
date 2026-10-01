"""Shared contract between the ingest side (parsers) and the engine side (compile/query)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParsedDocument:
    """Output of any parser. Text is plain UTF-8; PII masking (if enabled) is already applied."""

    title: str
    text: str
    source_name: str  # original file name, e.g. "2026-09-30-weekly.eml"
    metadata: dict[str, str] = field(default_factory=dict)  # e.g. {"date": "2026-09-30", "from": "..."}


@dataclass(frozen=True)
class RawRecord:
    """A document saved under raw/ with its access label. `space` is mandatory (fail closed)."""

    raw_path: str  # project-data-relative, e.g. "raw/dept-a/2026-09-30-weekly.md"
    space: str  # e.g. "dept-a" or "dept-a/part-1"
    sha256: str
