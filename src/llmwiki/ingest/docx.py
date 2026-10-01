"""DOCX parser using only zipfile + xml (no third-party deps).

Paragraphs and tables are emitted in document order. Tables render as markdown with merged cells
(gridSpan / vMerge) expanded, nested tables follow their parent table, and text boxes, content controls,
form-field check boxes and headers/footers are included.
"""
from __future__ import annotations

import re
from pathlib import PurePath
from xml.etree import ElementTree as ET

from llmwiki.ingest.ooxml import core_properties, open_package, parse_xml, read_member
from llmwiki.ingest.tablefmt import render_table
from llmwiki.models import ParsedDocument

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_SKIP = {
    _W + n for n in ("pPr", "rPr", "del", "instrText", "delText", "sdtPr", "sdtEndPr", "tblPr", "trPr", "tcPr")
} | {_MC + "Fallback"}
_TRANSPARENT = {_W + "sdt", _W + "sdtContent", _W + "customXml", _W + "ins", _W + "smartTag"}
_MAX_SPAN = 200
_MAX_CELLS = 200000
_MAX_DEPTH = 8
_HDR = re.compile(r"^word/(header|footer)\d*\.xml$")


def _walk(el: ET.Element, parts: list[str], boxes: list[ET.Element]) -> None:
    for ch in el:
        t = ch.tag
        if t == _W + "t":
            parts.append(ch.text or "")
        elif t == _W + "tab":
            parts.append("\t")
        elif t in (_W + "br", _W + "cr"):
            parts.append("\n")
        elif t == _W + "noBreakHyphen":
            parts.append("-")
        elif t == _W + "txbxContent":
            boxes.append(ch)
        elif t == _W + "ffData":  # legacy form-field check box
            cb = ch.find(_W + "checkBox")
            if cb is not None:
                chk = cb.find(_W + "checked")
                dflt = cb.find(_W + "default")
                on = (chk is not None and chk.get(_W + "val", "1") not in ("0", "false")) or (
                    chk is None and dflt is not None and dflt.get(_W + "val", "1") not in ("0", "false"))
                parts.append("☑" if on else "☐")
        elif t not in _SKIP:
            _walk(ch, parts, boxes)


def _paragraph(p: ET.Element, depth: int) -> list[str]:
    """Text of one paragraph followed by the paragraphs of any text boxes it anchors."""
    parts: list[str] = []
    boxes: list[ET.Element] = []
    _walk(p, parts, boxes)
    out = ["".join(parts)]
    if depth < _MAX_DEPTH:
        for box in boxes:
            out.extend(_blocks(box, depth + 1))
    return out


def _cell_text(tc: ET.Element, depth: int, nested: list[ET.Element]) -> str:
    paras: list[str] = []

    def visit(container: ET.Element) -> None:
        for ch in container:
            if ch.tag == _W + "p":
                paras.extend(s.strip() for s in _paragraph(ch, depth) if s.strip())
            elif ch.tag == _W + "tbl":
                nested.append(ch)
            elif ch.tag in _TRANSPARENT:
                visit(ch)

    visit(tc)
    return "\n".join(paras)


def _int(el: ET.Element | None, default: int) -> int:
    try:
        return max(0, min(int(el.get(_W + "val", default)), _MAX_SPAN)) if el is not None else default
    except ValueError:
        return default


def _rows(tbl: ET.Element) -> list[ET.Element]:
    out: list[ET.Element] = []

    def visit(el: ET.Element) -> None:
        for ch in el:
            if ch.tag == _W + "tr":
                out.append(ch)
            elif ch.tag in _TRANSPARENT:
                visit(ch)

    visit(tbl)
    return out


def _cells(tr: ET.Element) -> list[ET.Element]:
    out: list[ET.Element] = []

    def visit(el: ET.Element) -> None:
        for ch in el:
            if ch.tag == _W + "tc":
                out.append(ch)
            elif ch.tag in _TRANSPARENT:
                visit(ch)

    visit(tr)
    return out


def _table(tbl: ET.Element, depth: int) -> str:
    """Render a table (merge-expanded) followed by its nested tables."""
    grid: list[list[str]] = []
    merges: list[list[int]] = []  # [row, col, rowspan, colspan]
    vopen: dict[int, list[int]] = {}
    nested_out: list[tuple[int, int, ET.Element]] = []
    total = 0
    for r, tr in enumerate(_rows(tbl)):
        row: list[str] = [""] * _int(tr.find(f"{_W}trPr/{_W}gridBefore"), 0)
        for tc in _cells(tr):
            total += 1
            if total > _MAX_CELLS:
                break
            col = len(row)
            pr = tc.find(_W + "tcPr")
            span = max(1, _int(pr.find(_W + "gridSpan") if pr is not None else None, 1))
            vm = pr.find(_W + "vMerge") if pr is not None else None
            nested: list[ET.Element] = []
            text = _cell_text(tc, depth, nested)
            if vm is not None and vm.get(_W + "val", "continue") != "restart":
                above = grid[r - 1] if r else []
                text = above[col] if col < len(above) else ""
                if col in vopen:
                    vopen[col][2] += 1
                else:
                    vopen[col] = [r - 1, col, 2, span]
                    merges.append(vopen[col])
            else:
                for c in range(col, col + span):
                    vopen.pop(c, None)
                if vm is not None:
                    vopen[col] = [r, col, 1, span]
                    merges.append(vopen[col])
                elif span > 1:
                    merges.append([r, col, 1, span])
                nested_out.extend((r + 1, col + 1, n) for n in nested)
            row.extend([text] * span)
        grid.append(row)
    out = render_table(grid, [tuple(m) for m in merges])  # type: ignore[misc]
    if depth < _MAX_DEPTH:
        for r, c, n in nested_out:
            sub = _table(n, depth + 1)
            if sub:
                out += f"\n\n(중첩 표: {r}행 {c}열)\n\n{sub}"
    return out


def _blocks(container: ET.Element, depth: int = 0) -> list[str]:
    out: list[str] = []
    for ch in container:
        if ch.tag == _W + "p":
            out.extend(s.strip() for s in _paragraph(ch, depth) if s.strip())
        elif ch.tag == _W + "tbl":
            t = _table(ch, depth)
            if t:
                out.append(t)
        elif ch.tag in _TRANSPARENT:
            out.extend(_blocks(ch, depth))
    return out


def parse_docx(data: bytes, source_name: str, max_bytes: int | None = None) -> ParsedDocument:
    """Extract text (paragraphs, tables with merged cells expanded, text boxes, headers/footers) and core properties.

    Rejects oversized input, zip bombs (declared uncompressed size per member / in total > limit)
    and XML with DTD/entity declarations.
    """
    z, limit = open_package(data, source_name, "docx", max_bytes)
    with z:
        names = z.namelist()
        if "word/document.xml" not in names:
            raise ValueError(f"not a valid .docx: {source_name}")
        body = parse_xml(read_member(z, "word/document.xml", limit), "word/document.xml")
        title, metadata = core_properties(z, limit)
        extra: list[str] = []
        for n in sorted(n for n in names if _HDR.match(n)):
            for b in _blocks(parse_xml(read_member(z, n, limit), n)):
                if b not in extra:
                    extra.append(b)
    blocks = _blocks(body.find(_W + "body") if body.find(_W + "body") is not None else body) + extra
    if not title:
        title = PurePath(source_name).stem
    return ParsedDocument(title=title, text="\n\n".join(blocks), source_name=source_name, metadata=metadata)
