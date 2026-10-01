"""Excel number-format rendering: show cell values the way a user sees them (dates, %, thousands, currency)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_BUILTIN = {
    1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%",
    37: "#,##0;(#,##0)", 38: "#,##0;(#,##0)", 39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;(#,##0.00)",
    41: "#,##0", 42: "#,##0", 43: "#,##0.00", 44: "#,##0.00",
}
_DATE_IDS = {14, 15, 16, 17, 22, *range(27, 37), *range(50, 59)}
_TIME_IDS = {18, 19, 20, 21, 45, 46, 47}
_STRIP = re.compile(r'"[^"]*"|\[[^\]]*\]|\.|_.|\*.')


def kind_of(fmt_id: int, code: str | None) -> tuple[str, str]:
    """Classify a number format -> ("date"|"time"|"num", format code)."""
    if code is None:
        if fmt_id in _DATE_IDS:
            return "date", ""
        if fmt_id in _TIME_IDS:
            return "time", ""
        return "num", _BUILTIN.get(fmt_id, "General")
    if code.lower() in ("general", "standard", "@", ""):
        return "num", "General"
    bare = _STRIP.sub("", code).lower()
    if "y" in bare or "d" in bare:
        return "date", code
    if "h" in bare or "s" in bare:
        return "time", code
    if "m" in bare and "0" not in bare and "#" not in bare:
        return "date", code
    return "num", code


def general(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return format(v, ".15g")


def format_date(serial: float, kind: str, date1904: bool) -> str:
    if serial < 0 or serial > 2958465:
        return general(serial)
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    dt = base + timedelta(seconds=round(serial * 86400))
    has_time = dt.hour or dt.minute or dt.second
    if kind == "time" and serial < 1:
        return dt.strftime("%H:%M")
    if has_time:
        return dt.strftime("%Y-%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d")


def _sections(code: str) -> list[str]:
    out, cur, quoted = [], [], False
    for ch in code:
        if ch == '"':
            quoted = not quoted
        if ch == ";" and not quoted:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _tokens(sec: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    i = 0
    while i < len(sec):
        ch = sec[i]
        if ch == '"':
            j = sec.find('"', i + 1)
            j = len(sec) if j < 0 else j
            toks.append(("lit", sec[i + 1:j]))
            i = j
        elif ch == "\\" and i + 1 < len(sec):
            i += 1
            toks.append(("lit", sec[i]))
        elif ch in "_*":
            i += 1
        elif ch == "[":
            j = sec.find("]", i)
            j = len(sec) - 1 if j < 0 else j
            inner = sec[i + 1:j]
            if inner.startswith("$"):
                toks.append(("lit", inner[1:].split("-")[0]))
            i = j
        elif ch in "0#?":
            toks.append(("ph", ch))
        elif ch == ".":
            toks.append(("dot", ch))
        elif ch == ",":
            toks.append(("comma", ch))
        elif ch == "%":
            toks.append(("pct", "%"))
        else:
            toks.append(("lit", ch))
        i += 1
    return toks


def format_number(v: float, code: str) -> str:
    if code in ("", "General") or "E+" in code.upper() or "E-" in code.upper():
        return general(v)
    secs = _sections(code)
    sec, auto_neg = secs[0], True
    if v < 0 and len(secs) > 1:
        sec, v, auto_neg = secs[1], -v, False
    elif v == 0 and len(secs) > 2:
        sec = secs[2]
    toks = _tokens(sec)
    idx = [k for k, (t, _) in enumerate(toks) if t in ("ph", "dot", "comma")]
    if not idx or not any(t == "ph" for t, _ in toks):
        return general(v)
    first, last = idx[0], idx[-1]
    span = toks[first:last + 1]
    prefix = "".join(x for t, x in toks[:first] if t in ("lit", "pct"))
    suffix = "".join(x for t, x in toks[last + 1:] if t in ("lit", "pct"))
    pct = sum(t == "pct" for t, _ in toks)
    dot = next((k for k, (t, _) in enumerate(span) if t == "dot"), None)
    ints = span if dot is None else span[:dot]
    decs = [] if dot is None else span[dot + 1:]
    last_ph = max((k for k, (t, _) in enumerate(ints) if t == "ph"), default=-1)
    scale = sum(t == "comma" for t, _ in ints[last_ph + 1:]) if dot is None else 0
    thousands = any(t == "comma" for t, _ in ints[:last_ph + 1])
    max_dec = sum(t == "ph" for t, _ in decs)
    min_dec = sum(x in "0?" for t, x in decs if t == "ph")
    int_min = sum(x == "0" for t, x in ints if t == "ph")
    try:
        d = Decimal(repr(float(v))) * (Decimal(100) ** pct) / (Decimal(1000) ** scale)
        q = d.quantize(Decimal(1).scaleb(-max_dec), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return general(v)
    s = format(abs(q), ",f" if thousands else "f")
    whole, _, frac = s.partition(".")
    frac = frac.rstrip("0").ljust(min_dec, "0")
    if int_min > len(whole) and not thousands:
        whole = whole.zfill(int_min)
    sign = "-" if (q < 0 and auto_neg) else ""
    return sign + prefix + whole + ("." + frac if frac else "") + suffix
