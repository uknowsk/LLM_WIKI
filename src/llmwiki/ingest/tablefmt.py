"""Render a (merge-expanded) cell grid as a markdown table plus a flattened "path" listing.

Callers expand merged cells themselves (every cell of a merged range already carries the top-left value)
and pass the merge ranges as (row, col, rowspan, colspan) so multi-row headers can be detected.
"""
from __future__ import annotations

import re

Merge = tuple[int, int, int, int]

_NUM = re.compile(r"^[\s$₩€¥£+\-(]*\d[\d,]*(?:\.\d+)?\s*[%)원]*$")
FLAT_HEADING = "### 항목별 값"


def _is_num(v: str) -> bool:
    return bool(_NUM.match(v))


def esc(v: str) -> str:
    return v.replace("\r\n", "\n").replace("\r", "\n").replace("|", "\\|").replace("\n", "<br>")


_DATE = re.compile(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_MAX_HEADER_ROW = 15


def _looks_header(row: list[str]) -> bool:
    non = [c for c in row if c]
    return bool(non) and len(non) * 2 >= len(row) and sum(map(_is_num, non)) * 2 < len(non)


def _textlike(v: str) -> bool:
    """Header-compatible cell: non-empty, not a number/date (a bare year such as 2026 is allowed)."""
    return bool(v) and (bool(_YEAR.match(v)) or not (_is_num(v) or _DATE.match(v)))


def _plausible_header(grid: list[list[str]], merges: list[Merge], i: int) -> bool:
    """Row i can start the table: mostly text cells across the columns used from row i down, not a
    continuation of a vertical merge, and followed by a data row of similar width."""
    row = grid[i]
    cols = [c for c in range(len(row)) if row[c]]
    if not cols:
        return False
    if any(r0 < i < r0 + rs and c0 <= c < c0 + cs for r0, c0, rs, cs in merges for c in cols):
        return False
    used = [c for c in range(len(row)) if any(r[c] for r in grid[i:])]
    span = cols[-1] - cols[0] + 1
    if len(cols) < 0.7 * len(used) or len(cols) < 0.7 * span or (len(cols) == 1 and len(used) > 1):
        return False
    if any(len(row[c]) > 40 or "\n" in row[c] or _DATE.match(row[c]) for c in cols):  # labels are short; dates are values
        return False
    if sum(map(_textlike, (row[c] for c in cols))) < 0.7 * len(used):
        return False
    n = len(row)  # `label | value merged to the end` rows at/near i mark a key-value form, not a header
    nxt = next((r for r in grid[i + 1:] if any(r)), None)
    for r0, c0, rs, cs in merges:
        if n >= 3 and rs == 1 and c0 == 1 and cs >= 2 and c0 + cs >= n:
            if i < r0 <= i + 3 or (r0 == i and (nxt is None or len(set(nxt[c0:c0 + cs])) < 2 or not any(m[0] == i and m[2] > 1 for m in merges))):
                return False
    return nxt is not None and sum(1 for c in cols if nxt[c]) * 2 >= len(cols)


def _plain_line(grid: list[list[str]], merges: list[Merge], r: int) -> str:
    """A row above the header as a plain line: `label: value` pairs / `a / b / c` groups joined by ` · `."""
    cont = {(rr, c) for r0, c0, rs, cs in merges for rr in range(r0 + 1, r0 + rs) for c in range(c0, c0 + cs)}
    segs: list[list[str]] = [[]]
    for c, v in enumerate(grid[r]):
        if v and (r, c) not in cont:
            if not segs[-1] or segs[-1][-1] != v:
                segs[-1].append(v)
        elif segs[-1]:
            segs.append([])
    return " · ".join(f"{g[0]}: {g[1]}" if len(g) == 2 else " / ".join(g) for g in segs if g)


def _path(parts: list[str]) -> str:
    out: list[str] = []
    for p in parts:
        if p and (not out or out[-1] != p):
            out.append(p)
    return " > ".join(out)


def render_table(grid: list[list[str]], merges: list[Merge]) -> str:
    """Markdown table (+ `### 항목별 값` section for multi-level headers / vertically merged labels)."""
    grid = [[(c or "").strip() for c in row] for row in grid]
    if not any(any(r) for r in grid):
        return ""
    ncols = max(len(r) for r in grid)
    grid = [r + [""] * (ncols - len(r)) for r in grid]
    lines: list[str] = []
    off = 0
    while off < 3 and len(grid) - off >= 3 and ncols > 1:  # full-width merged title rows -> plain lines
        filled = [c for c in grid[off] if c]
        if filled and len(set(filled)) == 1 and any(m[0] == off and m[1] == 0 and m[2] == 1 and m[3] >= ncols for m in merges):
            lines.append(filled[0])
            off += 1
        else:
            break
    # header row: first plausible row in the top rows (title / metadata / approver rows above it become plain lines)
    hr = next((i for i in range(off, min(off + _MAX_HEADER_ROW, len(grid) - 1)) if _plausible_header(grid, merges, i)), None)
    top = off if hr is None else hr
    lines += [ln for r in range(off, top) if (ln := _plain_line(grid, merges, r))]
    grid = grid[top:]
    merges = [(m[0] - top, *m[1:]) for m in merges if m[0] >= top]

    h = 0
    if hr is not None:
        h = 1
        for _ in range(3):
            nh = h
            for r0, _c0, rs, _cs in merges:
                if r0 < h < r0 + rs:
                    nh = max(nh, r0 + rs)
            if nh == h and h < len(grid) and any(m[0] == h - 1 and 1 < m[3] < ncols for m in merges) and _looks_header(grid[h]):
                nh = h + 1
            nh = min(nh, len(grid))
            if nh == h:
                break
            h = nh

    keep = [c for c in range(ncols) if any(r[c] for r in grid)]
    if h:
        heads = [_path([grid[r][c] for r in range(h)]) or f"열 {n}" for n, c in enumerate(keep, 1)]
    else:
        heads = [f"열 {n}" for n in range(1, len(keep) + 1)]
    data = [r for r in grid[h:] if any(r)]

    tbl = ["| " + " | ".join(esc(x) for x in heads) + " |", "|" + " --- |" * len(keep)]
    for r in data:
        tbl.append("| " + " | ".join(esc(r[c]) for c in keep) + " |")
    out = "\n".join(lines) + "\n\n" if lines else ""
    out += "\n".join(tbl)

    vcols = {c for r0, c0, rs, cs in merges if rs > 1 and r0 >= h for c in range(c0, c0 + cs)}
    if h and (h >= 2 or vcols) and data and len(keep) > 1:
        nlab = 0
        for c in keep[: min(3, len(keep) - 1)]:
            vals = [r[c] for r in data if r[c]]
            if c in vcols or (vals and sum(map(_is_num, vals)) * 10 <= len(vals) * 3):
                nlab += 1
            else:
                break
        labels, values = keep[:nlab], keep[nlab:]
        flat = []
        for n, r in enumerate(data, 1):
            lab = " / ".join(dict.fromkeys(r[c] for c in labels if r[c])) or f"행 {n}"
            seen = {r[c] for c in labels}  # a merged total row repeats its label in every column
            pairs = [f"{heads[keep.index(c)]}: {r[c]}" for c in values if r[c] and r[c] not in seen]
            if pairs:
                flat.append(f"- {lab} | " + "; ".join(pairs))
        if flat:
            out += f"\n\n{FLAT_HEADING}\n" + "\n".join(flat)
    return out
