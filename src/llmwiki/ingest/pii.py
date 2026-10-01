"""Regex PII masking (Korean RRN, phone, email, card). Applied only when enabled by the caller."""
from __future__ import annotations

import re
from dataclasses import replace

from llmwiki.models import ParsedDocument

_D = r"(?<![\d])"  # not preceded by a digit
_E = r"(?![\d])"  # not followed by a digit

# Order matters: more specific patterns first so they are not swallowed by looser ones.
_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("email", "[EMAIL]", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("rrn", "[RRN]", re.compile(_D + r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[-\s]?[1-8]\d{6}" + _E)),
    ("card", "[CARD]", re.compile(_D + r"\d{4}([-\s]?)\d{4}\1\d{4}\1\d{4}" + _E)),
    ("phone", "[PHONE]", re.compile(_D + r"(?:\+82[-\s]?)?0(?:1[016-9]|2|[3-6]\d|70)[-\s.]?\d{3,4}[-\s.]?\d{4}" + _E)),
)


def mask(text: str) -> tuple[str, dict[str, int]]:
    """Return (masked text, counts per PII kind). Kinds with zero hits are omitted."""
    counts: dict[str, int] = {}
    for kind, token, pattern in _PATTERNS:
        text, n = pattern.subn(token, text)
        if n:
            counts[kind] = n
    return text, counts


def mask_document(doc: ParsedDocument) -> tuple[ParsedDocument, dict[str, int]]:
    """Mask text and title of a ParsedDocument; metadata is left untouched (needed for ACL/threading)."""
    text, counts = mask(doc.text)
    title, tcounts = mask(doc.title)
    for k, v in tcounts.items():
        counts[k] = counts.get(k, 0) + v
    return replace(doc, text=text, title=title), counts
