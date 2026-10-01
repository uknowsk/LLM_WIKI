"""Grounding Invariant lint: numbers, ISO dates and long quotes in an article must appear verbatim
in its linked raw files. Reports suspects only; never edits anything."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Settings
from . import article as art
from .store import Store

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_QUOTE = re.compile(r"\"([^\"\n]{10,})\"|“([^”\n]{10,})”|「([^」\n]{10,})」")
_LINKS = re.compile(r"\[\[[^\]]*\]\]|\[[^\]]*\]\([^)]*\)")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Suspect:
    article: str
    kind: str  # "number" | "date" | "quote" | "missing-source" | "no-source"
    value: str


def _norm(s: str) -> str:
    return _WS.sub(" ", s)


def check_text(body: str, raw_text: str) -> list[tuple[str, str]]:
    """Return (kind, value) for each claim in `body` that is not verbatim in `raw_text`."""
    raw = _norm(raw_text)
    body = _LINKS.sub(" ", body)
    out: list[tuple[str, str]] = []
    for m in _QUOTE.finditer(body):
        q = next(g for g in m.groups() if g)
        if _norm(q) not in raw:
            out.append(("quote", q))
    body = _QUOTE.sub(" ", body)
    for d in _DATE.findall(body):
        if d not in raw:
            out.append(("date", d))
    body = _DATE.sub(" ", body)
    for n in _NUMBER.findall(body):
        n = n.rstrip(",")
        if (len(n) >= 2 or "." in n) and n not in raw:  # single digits are list/ordinal noise
            out.append(("number", n))
    return out


def lint_article(settings: Settings, store: Store, path: str) -> list[Suspect]:
    sources = store.article_sources(path)
    if not sources:
        return [Suspect(path, "no-source", "")]
    f = settings.wiki_dir / path
    body = art.split_body(f.read_text(encoding="utf-8"))
    raws, suspects = [], []
    for s in sources:
        rf = settings.data_dir / s
        if rf.is_file():
            raws.append(rf.read_text(encoding="utf-8"))
        else:
            suspects.append(Suspect(path, "missing-source", s))
    raw_all = "\n".join(raws)
    suspects += [Suspect(path, k, v) for k, v in check_text(body, raw_all)]
    return suspects


def lint_grounding(settings: Settings, store: Store) -> list[Suspect]:
    out: list[Suspect] = []
    for p in store.article_paths():
        out += lint_article(settings, store, p)
    return out
