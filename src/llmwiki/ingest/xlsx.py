"""XLSX parser (stdlib only: zipfile + xml.etree). Merged cells are expanded so every cell of a merged
range carries the top-left value; cached formula values are used (nothing is ever evaluated)."""
from __future__ import annotations

import posixpath
import re
from pathlib import PurePath
from xml.etree import ElementTree as ET

from llmwiki.ingest.numfmt import format_date, format_number, general, kind_of
from llmwiki.ingest.ooxml import core_properties, open_package, parse_xml, read_member
from llmwiki.ingest.tablefmt import render_table
from llmwiki.models import ParsedDocument

MAX_SHEETS = 50
MAX_CELLS_PER_SHEET = 20000
_MAX_MERGE_FILL = 200000
_REF = re.compile(r"^([A-Z]{1,3})(\d{1,7})$")


def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kids(el: ET.Element | None, name: str) -> list[ET.Element]:
    return [c for c in el if _ln(c.tag) == name] if el is not None else []


def _kid(el: ET.Element | None, name: str) -> ET.Element | None:
    return next(iter(_kids(el, name)), None)


def _rich_text(si: ET.Element) -> str:
    """Concatenate <t> and <r><t> runs of a shared/inline string (phonetic <rPh> runs are skipped)."""
    out = []
    for c in si:
        n = _ln(c.tag)
        if n == "t":
            out.append(c.text or "")
        elif n == "r":
            out.extend(t.text or "" for t in c if _ln(t.tag) == "t")
    return "".join(out)


def _resolve(base_dir: str, target: str) -> str | None:
    path = target.lstrip("/") if target.startswith("/") else posixpath.join(base_dir, target)
    path = posixpath.normpath(path)
    return None if path.startswith("..") or path.startswith("/") else path


def _rels(z, limit: int, path: str) -> dict[str, tuple[str, str]]:
    """rel id -> (type suffix, raw target)."""
    if path not in z.namelist():
        return {}
    root = parse_xml(read_member(z, path, limit), path)
    return {r.get("Id", ""): ((r.get("Type") or "").rsplit("/", 1)[-1], r.get("Target") or "") for r in root}


def _styles(z, limit: int, path: str) -> list[tuple[str, str]]:
    """cellXfs index -> (kind, format code)."""
    if path not in z.namelist():
        return []
    root = parse_xml(read_member(z, path, limit), path)
    custom = {}
    for nf in _kids(_kid(root, "numFmts"), "numFmt"):
        try:
            custom[int(nf.get("numFmtId", ""))] = nf.get("formatCode", "")
        except ValueError:
            pass
    out = []
    for xf in _kids(_kid(root, "cellXfs"), "xf"):
        try:
            fid = int(xf.get("numFmtId", "0"))
        except ValueError:
            fid = 0
        out.append(kind_of(fid, custom.get(fid)))
    return out


def _shared_strings(z, limit: int, path: str) -> list[str]:
    if path not in z.namelist():
        return []
    root = parse_xml(read_member(z, path, limit), path)
    return [_rich_text(si) for si in _kids(root, "si")]


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _cell_text(c: ET.Element, sst: list[str], styles: list[tuple[str, str]], d1904: bool) -> str:
    t = c.get("t", "n")
    if t == "inlineStr":
        is_ = _kid(c, "is")
        return _rich_text(is_).strip() if is_ is not None else ""
    v = _kid(c, "v")
    raw = (v.text or "").strip() if v is not None else ""
    if raw == "":
        return ""
    if t == "s":
        try:
            return sst[int(raw)].strip()
        except (ValueError, IndexError):
            return ""
    if t == "b":
        return "TRUE" if raw == "1" else "FALSE"
    if t in ("e", "str", "d"):
        return raw
    try:
        num = float(raw)
    except ValueError:
        return raw
    if num != num or num in (float("inf"), float("-inf")):
        return raw
    try:
        s = int(c.get("s", "0"))
    except ValueError:
        s = 0
    kind, code = styles[s] if 0 <= s < len(styles) else ("num", "General")
    if kind in ("date", "time"):
        return format_date(num, kind, d1904)
    return format_number(num, code)


def _read_sheet(root: ET.Element, sst, styles, d1904) -> tuple[str, bool]:
    cells: dict[tuple[int, int], str] = {}
    truncated = False
    seq_r = -1
    for row in _kids(_kid(root, "sheetData"), "row"):
        try:
            seq_r = int(row.get("r")) - 1 if row.get("r") else seq_r + 1
        except ValueError:
            seq_r += 1
        seq_c = -1
        for c in _kids(row, "c"):
            m = _REF.match(c.get("r", ""))
            col, r = (_col_index(m.group(1)), int(m.group(2)) - 1) if m else (seq_c + 1, seq_r)
            seq_c = col
            if col > 16383 or not 0 <= r <= 1048575:
                continue
            text = _cell_text(c, sst, styles, d1904)
            if text:
                if len(cells) >= MAX_CELLS_PER_SHEET:
                    truncated = True
                    break
                cells[(r, col)] = text
        if truncated:
            break
    if not cells:
        return "", truncated
    max_r = max(r for r, _ in cells)
    max_c = max(c for _, c in cells)
    spans = []
    budget = _MAX_MERGE_FILL
    for mcell in _kids(_kid(root, "mergeCells"), "mergeCell"):  # merge refs are untrusted: clip to used range
        a, _, b = mcell.get("ref", "").partition(":")
        ma, mb = _REF.match(a), _REF.match(b or a)
        if not (ma and mb):
            continue
        r0, r1 = int(ma.group(2)) - 1, min(int(mb.group(2)) - 1, max_r)
        c0, c1 = _col_index(ma.group(1)), min(_col_index(mb.group(1)), max_c)
        val = cells.get((r0, c0))
        n = (r1 - r0 + 1) * (c1 - c0 + 1)
        if not val or n <= 0 or n > budget:
            continue
        budget -= n
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                cells.setdefault((r, c), val)
        spans.append((r0, c0, r1 - r0 + 1, c1 - c0 + 1))
    rows = sorted({r for r, _ in cells})
    cols = sorted({c for _, c in cells})
    rmap = {r: i for i, r in enumerate(rows)}
    cmap = {c: i for i, c in enumerate(cols)}
    grid = [[cells.get((r, c), "") for c in cols] for r in rows]
    cmerges = []
    for r0, c0, rs, cs in spans:
        rr = [rmap[r] for r in range(r0, r0 + rs) if r in rmap]
        cc = [cmap[c] for c in range(c0, c0 + cs) if c in cmap]
        if rr and cc:
            cmerges.append((rr[0], cc[0], len(rr), len(cc)))
    return render_table(grid, cmerges), truncated


def parse_xlsx(data: bytes, name: str, *, max_bytes: int | None = None, include_hidden: bool = False) -> ParsedDocument:
    """Render every visible sheet as `## name` + markdown table (+ `### 항목별 값` for multi-level headers)."""
    z, limit = open_package(data, name, "xlsx", max_bytes)
    with z:
        names = set(z.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError(f"not a valid .xlsx: {name}")
        wb = parse_xml(read_member(z, "xl/workbook.xml", limit), "xl/workbook.xml")
        rels = _rels(z, limit, "xl/_rels/workbook.xml.rels")
        sst_path = next((p for t, p in rels.values() if t == "sharedStrings"), "sharedStrings.xml")
        sty_path = next((p for t, p in rels.values() if t == "styles"), "styles.xml")
        sst = _shared_strings(z, limit, _resolve("xl", sst_path) or "xl/sharedStrings.xml")
        styles = _styles(z, limit, _resolve("xl", sty_path) or "xl/styles.xml")
        pr = _kid(wb, "workbookPr")
        d1904 = pr is not None and pr.get("date1904", "0").lower() in ("1", "true")
        title, meta = core_properties(z, limit)
        blocks: list[str] = []
        listed: list[str] = []
        hidden: list[str] = []
        truncated: list[str] = []
        for i, sh in enumerate(_kids(_kid(wb, "sheets"), "sheet"), 1):
            sname = " ".join((sh.get("name") or f"Sheet{i}").split())
            if i > MAX_SHEETS:
                truncated.append(f"sheets>{MAX_SHEETS}")
                blocks.append(f"> 시트 {MAX_SHEETS}개 제한으로 이후 시트는 생략됨")
                break
            rid = next((v for k, v in sh.attrib.items() if k.endswith("}id")), "")
            target = rels.get(rid, ("", ""))[1]
            path = _resolve("xl", target) if target else f"xl/worksheets/sheet{i}.xml"
            if sh.get("state", "visible") != "visible":
                hidden.append(sname)
                if not include_hidden:
                    continue
            listed.append(sname)
            if not path or path not in names:
                continue
            body, trunc = _read_sheet(parse_xml(read_member(z, path, limit), path), sst, styles, d1904)
            if trunc:
                truncated.append(sname)
            note = f"\n\n> 이 시트는 {MAX_CELLS_PER_SHEET}셀 제한으로 일부만 포함됨" if trunc else ""
            blocks.append(f"## {sname}\n\n" + (body or "(빈 시트)") + note)
    if listed:
        meta["sheets"] = ", ".join(listed)
    if hidden:
        meta["hidden_sheets"] = ", ".join(hidden)
    if truncated:
        meta["truncated"] = ", ".join(truncated)
    return ParsedDocument(title=title or PurePath(name).stem, text="\n\n".join(blocks), source_name=name, metadata=meta)
