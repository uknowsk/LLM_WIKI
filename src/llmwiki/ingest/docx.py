"""DOCX parser using only zipfile + xml (no third-party deps)."""
from __future__ import annotations

import io
import zipfile
from pathlib import PurePath
from xml.etree import ElementTree as ET

from llmwiki.ingest.limits import check_size
from llmwiki.models import ParsedDocument

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_DCTERMS = "{http://purl.org/dc/terms/}"


def _paragraph_text(p: ET.Element) -> str:
    parts: list[str] = []
    for el in p.iter():
        if el.tag == _W + "t" and el.text:
            parts.append(el.text)
        elif el.tag == _W + "tab":
            parts.append("\t")
        elif el.tag in (_W + "br", _W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _read_member(z: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """Read one member with a hard cap (declared sizes can lie, so the read itself is bounded)."""
    with z.open(name) as f:
        out = f.read(limit + 1)
    if len(out) > limit:
        raise ValueError(f"docx member too large (zip bomb?): {name}")
    return out


def parse_docx(data: bytes, source_name: str, max_bytes: int | None = None) -> ParsedDocument:
    """Extract paragraph text (tables included, in document order) and core properties.

    Rejects oversized input and zip bombs (declared uncompressed size per member / in total > limit).
    """
    limit = check_size(data, source_name, max_bytes)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > 10000 or any(i.file_size > limit for i in infos) or sum(i.file_size for i in infos) > limit:
                raise ValueError(f"docx expands beyond the size limit (zip bomb?): {source_name}")
            body = ET.fromstring(_read_member(z, "word/document.xml", limit))
            core = ET.fromstring(_read_member(z, "docProps/core.xml", limit)) if "docProps/core.xml" in z.namelist() else None
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"not a valid .docx: {source_name}") from exc
    paras = [_paragraph_text(p) for p in body.iter(_W + "p")]
    text = "\n\n".join(p.strip() for p in paras if p.strip())
    title = ""
    metadata: dict[str, str] = {}
    if core is not None:
        title = (core.findtext(_DC + "title") or "").strip()
        created = (core.findtext(_DCTERMS + "created") or "").strip()
        if created:
            metadata["date"] = created[:10]
        creator = (core.findtext(_DC + "creator") or "").strip()
        if creator:
            metadata["author"] = creator
    if not title:
        title = PurePath(source_name).stem
    return ParsedDocument(title=title, text=text, source_name=source_name, metadata=metadata)
