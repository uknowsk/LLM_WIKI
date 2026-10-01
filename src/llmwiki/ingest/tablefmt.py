"""Render a (merge-expanded) cell grid as a markdown table plus a flattened "path" listing.

Callers expand merged cells themselves (every cell of a merged range already carries the top-left value)
and pass the merge ranges as (row, col, rowspan, colspan) so multi-row headers can be detected.
"""
from __future__ import annotations

import re

Merge = tuple[int, int, int, int]

_NUM = re.compile(r"^[\s$₩€¥£+\-(]*\d[\d,]*(?:\.\d+)?\s*[%)원]*$")
FLAT_HEADING = "### 항목별 값"


def col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _is_num(v: str) -> bool:
    return bool(_NUM.match(v))


def esc(v: str) -> str:
    return v.replace("\r\n", "\n").replace("\r", "\n").replace("|", "\\|").replace("\n", "<br>")


def _looks_header(row: list[str]) -> bool:
    non = [c for c in row if c]
    return bool(non) and len(non) * 2 >= len(row) and sum(map(_is_num, non)) * 2 < len(non)


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
    grid = grid[off:]
    merges = [(m[0] - off, *m[1:]) for m in merges if m[0] >= off]

    h = 0
    if _looks_header(grid[0]):
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
        heads = [_path([grid[r][c] for r in range(h)]) or col_letter(c) for c in keep]
    else:
        heads = [col_letter(c) for c in keep]
    data = [r for r in grid[h:] if any(r)]

    tbl = ["| " + " | ".join(esc(x) for x in heads) + " |", "|" + " --- |" * len(keep)]
    for r in data:
        tbl.append("| " + " | ".join(esc(r[c]) for c in keep) + " |")
    out = "\n".join(lines) + "\n\n" if lines else ""
    out += "\n".join(tbl)

    vcols = {c for r0, c0, rs, cs in merges if rs > 1 and r0 >= h for c in range(c0, c0 + cs)}
    if (h >= 2 or vcols) and data and len(keep) > 1:
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
            pairs = [f"{heads[keep.index(c)]}: {r[c]}" for c in values if r[c]]
            if pairs:
                flat.append(f"- {lab} | " + "; ".join(pairs))
        if flat:
            out += f"\n\n{FLAT_HEADING}\n" + "\n".join(flat)
    return out
