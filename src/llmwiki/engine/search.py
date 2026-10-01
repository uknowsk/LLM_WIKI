"""Tiny BM25 with Korean-friendly tokenization (character bigrams for CJK runs)."""
from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN = re.compile(r"[0-9A-Za-z_]+|[぀-ヿ㐀-鿿가-힯]+")
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for m in _TOKEN.findall(text.lower()):
        if _CJK.match(m):
            out.extend([m] if len(m) == 1 else [m[i:i + 2] for i in range(len(m) - 1)])
        else:
            out.append(m)
    return out


def bm25_rank(query: str, docs: dict[str, str], k1: float = 1.5, b: float = 0.75) -> list[tuple[str, float]]:
    """Rank `docs` (id -> text) for `query`. Only docs with score > 0 are returned, best first."""
    toks = {d: Counter(tokenize(t)) for d, t in docs.items()}
    if not toks:
        return []
    n = len(toks)
    avg = sum(sum(c.values()) for c in toks.values()) / n or 1.0
    q_terms = set(tokenize(query))
    df = {t: sum(1 for c in toks.values() if t in c) for t in q_terms}
    ranked = []
    for d, c in toks.items():
        dl = sum(c.values())
        score = 0.0
        for t in q_terms:
            f = c.get(t, 0)
            if f:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avg))
        if score > 0:
            ranked.append((d, score))
    return sorted(ranked, key=lambda x: (-x[1], x[0]))
