"""Pure metric functions: ranking metrics (article level), answer scoring, leak detection, latency stats.

Ranking inputs are `hit_docs`: for each ranked hit (best first) the set of gold-corpus doc ids its article
derives from (via article sources -> raw -> corpus file). `gold` is the set of gold doc ids of the question.
"""
from __future__ import annotations

import functools
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

NO_EVIDENCE = "근거 없음"

# -- ranking ------------------------------------------------------------------------------------


def _rel(hit_docs: Sequence[Iterable[str]], gold: set[str]) -> list[bool]:
    return [bool(gold & set(h)) for h in hit_docs]


def recall_at_k(hit_docs: Sequence[Iterable[str]], gold: set[str], k: int) -> float:
    """Share of gold docs covered by the sources of the top-k hits (binary for single-doc questions)."""
    if not gold:
        return 0.0
    covered: set[str] = set()
    for h in hit_docs[:k]:
        covered |= gold & set(h)
    return len(covered) / len(gold)


def hit_at_k(hit_docs: Sequence[Iterable[str]], gold: set[str], k: int) -> float:
    return 1.0 if any(_rel(hit_docs, gold)[:k]) else 0.0


def mrr(hit_docs: Sequence[Iterable[str]], gold: set[str]) -> float:
    for i, r in enumerate(_rel(hit_docs, gold), 1):
        if r:
            return 1.0 / i
    return 0.0


def ndcg_at_k(hit_docs: Sequence[Iterable[str]], gold: set[str], k: int, n_relevant: int | None = None) -> float:
    """Binary-relevance nDCG over articles. `n_relevant` = number of relevant articles that exist (the ideal
    ranking puts min(k, n_relevant) of them first); defaults to len(gold)."""
    rel = _rel(hit_docs, gold)[:k]
    dcg = sum(1.0 / math.log2(i + 1) for i, r in enumerate(rel, 1) if r)
    n = len(gold) if n_relevant is None else n_relevant
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(k, n) + 1))
    return dcg / ideal if ideal > 0 else 0.0


# -- text normalization / fact matching ---------------------------------------------------------

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_DATE_KO = re.compile(r"(?<!\d)(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_DATE_SEP = re.compile(r"(?<![\d.\x01])(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})(?!\d)")


def _date(m: re.Match) -> str:
    return f"\x01{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}\x01"


@functools.lru_cache(maxsize=8192)
def normalize(text: str) -> str:
    """Canonical form for fact matching: NFKC, lowercase, dates -> (y,m,d), thousands separators and ALL
    whitespace removed. '1,200만원' == '1200만원'; '2026년 9월 30일' == '2026-09-30' == '2026.9.30'; '45 분' == '45분'."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = _DATE_KO.sub(_date, t)
    t = _DATE_SEP.sub(_date, t)
    prev = None
    while prev != t:  # '1,234,567' needs repeated passes only when overlapping; cheap and safe
        prev, t = t, _THOUSANDS.sub("", t)
    return re.sub(r"\s+", "", t)


def fact_in(fact: str, answer: str) -> bool:
    """Normalized substring match with digit boundaries (fact '12' must not match inside '123')."""
    nf, na = normalize(fact), normalize(answer)
    if not nf:
        return False
    pat = re.escape(nf)
    if nf[0].isdigit():
        pat = r"(?<!\d)" + pat
    if nf[-1].isdigit():
        pat += r"(?!\d)"
    return re.search(pat, na) is not None


def fact_recall(facts: Sequence[str], answer: str) -> float:
    if not facts:
        return 1.0
    return sum(fact_in(f, answer) for f in facts) / len(facts)


def answer_correct(facts: Sequence[str], answer: str) -> bool:
    return all(fact_in(f, answer) for f in facts)


_REFUSAL_TRIM = " \t\r\n.!?。'\"`“”‘’[]()"


def is_refusal(answer: str) -> bool:
    """True iff the answer is the refusal marker (surrounding punctuation/whitespace tolerated)."""
    return re.sub(r"\s+", " ", (answer or "").strip(_REFUSAL_TRIM)) == NO_EVIDENCE


def citation_accurate(cited_docs: Sequence[Iterable[str]], gold: set[str]) -> bool:
    """Cited articles include (a source of) a gold doc."""
    return any(gold & set(c) for c in cited_docs)


# -- cross-space leak detection -----------------------------------------------------------------


def detect_leak(space: str, answer: str, citations_spaces: Mapping[str, Iterable[str]],
                foreign_facts: Iterable[str], cited_texts: Mapping[str, str] | None = None) -> list[str]:
    """Reasons for a HARD-FAIL leak (empty list = clean). Checks: (1) a cited/retrieved article whose source
    spaces are not exactly inside `space` (an article without any known space also counts); (2) a foreign-space
    fact in the answer; (3) a foreign-space fact in the text of a cited article."""
    reasons = []
    for path, sp in citations_spaces.items():
        sp = set(sp)
        if not sp or sp - {space}:
            reasons.append(f"cited article outside space: {path}")
    for f in foreign_facts:
        if fact_in(f, answer):
            reasons.append(f"foreign fact in answer: {f}")
    for path, txt in (cited_texts or {}).items():
        for f in foreign_facts:
            if fact_in(f, txt):
                reasons.append(f"foreign fact in cited article {path}: {f}")
    return reasons


# -- latency ------------------------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (p in 0..100); 0.0 for no data."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))]


def latency_stats(ms: Sequence[float]) -> dict[str, float]:
    return {"p50": percentile(ms, 50), "p95": percentile(ms, 95), "mean": (sum(ms) / len(ms)) if ms else 0.0}
