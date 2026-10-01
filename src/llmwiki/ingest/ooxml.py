"""Shared OOXML helpers (zip + XML hardening) for the docx and xlsx parsers. Stdlib only."""
from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree as ET

from llmwiki.ingest.limits import check_size

_DC = "{http://purl.org/dc/elements/1.1/}"
_DCTERMS = "{http://purl.org/dc/terms/}"


def parse_xml(data: bytes, label: str) -> ET.Element:
    """Parse XML that came from an untrusted package.

    DTDs/entities are never legitimate in OOXML parts, so any DOCTYPE/ENTITY declaration is rejected
    up front (XXE and billion-laughs); UTF-16/32 encoded parts are rejected too, since the check is byte based.
    """
    head = data[:4]
    if b"\x00" in head or head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise ValueError(f"unsupported XML encoding in {label}")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError(f"XML DTD/entity declarations are not allowed: {label}")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML in {label}") from exc


def open_package(data: bytes, source_name: str, kind: str, max_bytes: int | None = None) -> tuple[zipfile.ZipFile, int]:
    """Size-check `data` and open it as a zip, rejecting zip bombs. Returns (zip, byte limit)."""
    limit = check_size(data, source_name, max_bytes)
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid .{kind}: {source_name}") from exc
    if len(infos) > 10000 or any(i.file_size > limit for i in infos) or sum(i.file_size for i in infos) > limit:
        raise ValueError(f"{kind} expands beyond the size limit (zip bomb?): {source_name}")
    return z, limit


def read_member(z: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """Read one member with a hard cap (declared sizes can lie, so the read itself is bounded)."""
    try:
        with z.open(name) as f:
            out = f.read(limit + 1)
    except (KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise ValueError(f"cannot read package member: {name}") from exc
    if len(out) > limit:
        raise ValueError(f"package member too large (zip bomb?): {name}")
    return out


def core_properties(z: zipfile.ZipFile, limit: int) -> tuple[str, dict[str, str]]:
    """(title, metadata) from docProps/core.xml; metadata has `date` (modified, else created) and `author`."""
    if "docProps/core.xml" not in z.namelist():
        return "", {}
    core = parse_xml(read_member(z, "docProps/core.xml", limit), "docProps/core.xml")
    meta: dict[str, str] = {}
    date = (core.findtext(_DCTERMS + "modified") or core.findtext(_DCTERMS + "created") or "").strip()
    if len(date) >= 10 and date[4] == "-" and date[7] == "-":
        meta["date"] = date[:10]
    creator = (core.findtext(_DC + "creator") or "").strip()
    if creator:
        meta["author"] = creator
    return (core.findtext(_DC + "title") or "").strip(), meta
