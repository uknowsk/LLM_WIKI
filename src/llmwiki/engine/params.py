"""Every retrieval/generation knob in one frozen dataclass (defaults == the shipped behavior)."""
from __future__ import annotations

from dataclasses import dataclass

MODES = ("bm25", "dense", "hybrid")


@dataclass(frozen=True)
class RetrievalParams:
    top_k: int = 5  # hits fed to the LLM
    candidate_pool: int = 50  # candidates kept per retriever before fusion
    rrf_k: int = 60  # reciprocal-rank-fusion constant
    w_bm25: float = 1.0  # fusion weights
    w_dense: float = 1.0
    min_cosine: float = 0.0  # dense candidates must have cosine > this (0.0: any positive similarity)
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    max_context_chars: int = 0  # per-article cap in the prompt; 0 = unlimited
    temperature: float | None = None  # None = the LLM client's own default
    retrieval_mode: str = "hybrid"  # "bm25" | "dense" | "hybrid" (hybrid/dense degrade without an embedder)

    def __post_init__(self) -> None:
        if self.retrieval_mode not in MODES:
            raise ValueError(f"retrieval_mode must be one of {MODES}")
        if self.top_k < 1 or self.candidate_pool < 1 or self.rrf_k < 0 or self.max_context_chars < 0:
            raise ValueError("top_k/candidate_pool must be >= 1; rrf_k/max_context_chars >= 0")


@dataclass(frozen=True)
class RetrievedHit:
    path: str
    score: float
    rank: int  # 1-based
