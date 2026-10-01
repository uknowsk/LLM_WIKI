"""Plain text / Markdown parser."""
from __future__ import annotations

import codecs
import re
from pathlib import PurePath

from llmwiki.ingest.limits import check_size
from llmwiki.models import ParsedDocument

_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)


def decode_bytes(data: bytes) -> str:
    """Decode by BOM (UTF-32/16/8), else UTF-8, falling back to CP949 (common for Korean Windows files)."""
    for bom, enc in (
        (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),  # before UTF-16 (shared prefix)
        (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                break
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_text(data: bytes, source_name: str, max_bytes: int | None = None) -> ParsedDocument:
    """Parse .md/.txt bytes. Title = first H1 heading, else the file stem."""
    check_size(data, source_name, max_bytes)
    text = decode_bytes(data).replace("\r\n", "\n").replace("\r", "\n").strip()
    m = _H1.search(text)
    title = m.group(1) if m else PurePath(source_name).stem
    return ParsedDocument(title=title, text=text, source_name=source_name)
