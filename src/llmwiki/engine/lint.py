"""Grounding Invariant lint: numbers, dates and long quotes in an article must appear in its linked
raw files (numbers compared normalized and on token boundaries, dates as (y, m, d)).
Reports suspects only; never edits anything."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ..config import Settings
from . import article as art
from .fsutil import read_text
from .store import Store

_DATE_PATTERNS = (
    re.compile(r"(?<![\d.\-/])(\d{4})-(\d{2})-(\d{2})(?![\d])"),
    re.compile(r"(?<!\d)(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"),
    re.compile(r"(?<![\d.])(\d{4})\.(\d{1,2})\.(\d{1,2})(?![\d.])"),
    re.compile(r"(?<![\d/])(\d{4})/(\d{1,2})/(\d{1,2})(?![\d/])"),
)
_NUMBER = re.compile(r"(?<![\d,.])\d[\d,]*(?:\.\d+)?(?![\d,])")
_QUOTE = re.compile(
    r"\"([^\"\n]{10,})\"|“([^”\n]{10,})”|「([^」\n]{10,})」|‘([^’\n]{10,})’|『([^』\n]{10,})』"
)
_LINKS = re.compile(r"\[\[[^\]]*\]\]|\[[^\]]*\]\([^)]*\)")
_FOOTER = re.compile(r"(?m)^[ \t]*\(Source: raw/[^\n]*\)[ \t]*$")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Suspect:
    article: str
    kind: str  # "number" | "date" | "quote" | "missing-source" | "invalid-source" | "missing-article" | "no-source"
    value: str


def _norm(s: str) -> str:
    return _WS.sub(" ", s)


def _num(token: str) -> str | None:
    """Canonical form: no thousands commas, no leading zeros / trailing fraction zeros."""
    try:
        return format(Decimal(token.replace(",", "")).normalize(), "f")
    except InvalidOperation:
        return None


def _dates(text: str) -> list[tuple[re.Match, tuple[int, int, int]]]:
    return [(m, tuple(int(g) for g in m.groups())) for p in _DATE_PATTERNS for m in p.finditer(text)]


def check_text(body: str, raw_text: str) -> list[tuple[str, str]]:
    """Return (kind, value) for each claim in `body` that is not grounded in `raw_text`."""
    raw = _norm(raw_text)
    body = _FOOTER.sub(" ", body)
    body = _LINKS.sub(" ", body)
    out: list[tuple[str, str]] = []
    for m in _QUOTE.finditer(body):
        q = next(g for g in m.groups() if g)
        if _norm(q) not in raw:
            out.append(("quote", q))
    body = _QUOTE.sub(" ", body)

    raw_dates = {t for _, t in _dates(raw_text)}
    for m, t in _dates(body):
        if t not in raw_dates:
            out.append(("date", m.group(0)))
    for p in _DATE_PATTERNS:
        body = p.sub(" ", body)

    raw_nums = {n for t in _NUMBER.findall(raw_text) if (n := _num(t.rstrip(","))) is not None}
    for t in _NUMBER.findall(body):
        t = t.rstrip(",")
        n = _num(t)
        if n is None or not (len(t) >= 2 or "." in t):  # single digits are list/ordinal noise
            continue
        if n not in raw_nums:
            out.append(("number", t))
    return out


def lint_article(settings: Settings, store: Store, path: str) -> list[Suspect]:
    sources = store.article_sources(path)
    if not sources:
        return [Suspect(path, "no-source", "")]
    try:
        body = art.split_body(read_text(settings.wiki_dir / path))
    except OSError:
        return [Suspect(path, "missing-article", path)]
    raw_root = (settings.data_dir / "raw").resolve()
    raws, suspects = [], []
    for s in sources:
        try:
            rf = (settings.data_dir / s).resolve()
            if not rf.is_relative_to(raw_root):
                suspects.append(Suspect(path, "invalid-source", s))
            elif rf.is_file():
                raws.append(read_text(rf))
            else:
                suspects.append(Suspect(path, "missing-source", s))
        except (OSError, ValueError):
            suspects.append(Suspect(path, "invalid-source", s))
    raw_all = "\n".join(raws)
    suspects += [Suspect(path, k, v) for k, v in check_text(body, raw_all)]
    return suspects


_DISPUTED = re.compile(r"(?m)^## Disputed[ \t]*$")  # the heading the compiler appends when sources conflict


def is_disputed(text: str) -> bool:
    return _DISPUTED.search(text) is not None


def lint_disputed(settings: Settings, store: Store) -> list[str]:
    """Paths of every article that carries a Disputed section (for admins to resolve). No ACL filter: this is an
    internal report and must never be exposed to end users."""
    out = []
    for p in sorted(store.article_paths()):
        try:
            if is_disputed(read_text(settings.wiki_dir / p)):
                out.append(p)
        except OSError:
            continue
    return out


def lint_grounding(settings: Settings, store: Store) -> list[Suspect]:
    out: list[Suspect] = []
    for p in store.article_paths():
        out += lint_article(settings, store, p)
    return out
