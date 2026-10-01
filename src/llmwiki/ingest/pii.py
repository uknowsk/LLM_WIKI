"""Regex PII masking (Korean RRN, phone, email, card, business no.). Applied only when enabled by the caller.

Matching runs on an NFKC-normalized copy with zero-width/format characters removed and dash
variants folded to "-", so obfuscation (full-width digits, U+200B, en dashes) cannot evade it.
Only the normalized+masked copy is returned.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from llmwiki.models import ParsedDocument

_D = r"(?<![\d])"  # not preceded by a digit
_E = r"(?![\d])"  # not followed by a digit
_S = r"[-\s.]?"
_AREA = r"(?:1[016-9]|2|[3-6]\d|70)"

# Order matters: more specific patterns first so they are not swallowed by looser ones.
_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    # local part bounded ({1,64}) so long runs cannot cause quadratic backtracking
    ("email", "[EMAIL]", re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,}")),
    ("rrn", "[RRN]", re.compile(_D + r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[-\s]{0,2}[1-8]\d{6}" + _E)),
    ("card", "[CARD]", re.compile(_D + r"\d{4}([-\s]?)\d{4}\1\d{4}\1\d{4}" + _E)),
    ("card", "[CARD]", re.compile(_D + r"\d{4}([-\s]?)\d{6}\1\d{5}" + _E)),  # 15-digit Amex
    ("brn", "[BRN]", re.compile(_D + r"\d{3}-\d{2}-\d{5}" + _E)),  # business registration number
    ("phone", "[PHONE]", re.compile(  # +82 / 82 international form
        _D + r"(?:\+\s?82|82)" + _S + r"(?:\(0\)" + _S + r")?0?" + _AREA + _S + r"\d{3,4}" + _S + r"\d{4}" + _E)),
    ("phone", "[PHONE]", re.compile(_D + r"\(0" + _AREA + r"\)" + _S + r"\d{3,4}" + _S + r"\d{4}" + _E)),
    ("phone", "[PHONE]", re.compile(_D + r"0" + _AREA + _S + r"\d{3,4}" + _S + r"\d{4}" + _E)),
)

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−﹘﹣－"), "-")


def normalize(text: str) -> str:
    """NFKC, fold dash variants to '-', drop zero-width / format (Cf) characters."""
    text = unicodedata.normalize("NFKC", text).translate(_DASHES)
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def mask(text: str) -> tuple[str, dict[str, int]]:
    """Return (masked normalized text, counts per PII kind). Kinds with zero hits are omitted."""
    text = normalize(text)
    counts: dict[str, int] = {}
    for kind, token, pattern in _PATTERNS:
        text, n = pattern.subn(token, text)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return text, counts


def mask_one_line(value: str) -> tuple[str, dict[str, int]]:
    """Mask and collapse to a single line (for header/metadata values)."""
    text, counts = mask(value)
    return " ".join(text.split()), counts


def mask_document(doc: ParsedDocument) -> tuple[ParsedDocument, dict[str, int]]:
    """Mask text, title, source_name and metadata values (keys kept). Callers keep the original for threading."""
    counts: dict[str, int] = {}

    def run(fn, value: str) -> str:
        out, c = fn(value)
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
        return out

    text = run(mask, doc.text)
    title = run(mask_one_line, doc.title)
    source = run(mask_one_line, doc.source_name)
    meta = {k: run(mask_one_line, v) for k, v in doc.metadata.items()}
    return replace(doc, text=text, title=title, source_name=source, metadata=meta), counts
