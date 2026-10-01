"""Plain text / Markdown parser."""
from __future__ import annotations

import re
from pathlib import PurePath

from llmwiki.models import ParsedDocument

_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)


def decode_bytes(data: bytes) -> str:
    """Decode UTF-8 (with/without BOM), falling back to CP949 (common for Korean Windows files)."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_text(data: bytes, source_name: str) -> ParsedDocument:
    """Parse .md/.txt bytes. Title = first H1 heading, else the file stem."""
    text = decode_bytes(data).replace("\r\n", "\n").replace("\r", "\n").strip()
    m = _H1.search(text)
    title = m.group(1) if m else PurePath(source_name).stem
    return ParsedDocument(title=title, text=text, source_name=source_name)
