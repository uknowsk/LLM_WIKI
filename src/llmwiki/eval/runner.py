"""Evaluation runner: opens a data dir snapshot and scores retrieval / answers per question."""
from __future__ import annotations

import dataclasses
import os
import time
from collections.abc import Sequence
from pathlib import Path

from ..audit import AuditLog
from ..auth import User
from ..config import Settings, load_settings
from ..engine.embed import Embedder
from ..engine.fsutil import read_text
from ..engine.llm import LLMClient
from ..engine.params import RetrievalParams
from ..engine.query import QueryService
from ..engine.store import Store
from . import metrics as m
from .dataset import QA, GoldMap


class _NoLLM:
    def complete(self, system: str, prompt: str, temperature: float | None = None) -> str:
        raise RuntimeError("answer mode needs an LLM client")


class PrecomputedEmbedder:
    """Query vectors are fetched ONCE up front (failing loudly) so a transient endpoint hiccup can never make
    the retriever silently fall back to BM25 in the middle of a tuning run."""

    def __init__(self, inner: Embedder):
        self.inner, self.model = inner, getattr(inner, "model", "")
        self.vecs: dict[str, list[float]] = {}

    def warm(self, questions: Sequence[str]) -> None:
        fn = getattr(self.inner, "embed_query", None)
        for q in dict.fromkeys(questions):
            if q not in self.vecs:
                self.vecs[q] = fn(q) if fn else self.inner.embed([q])[0]

    def embed_query(self, text: str) -> list[float]:
        if text not in self.vecs:
            self.warm([text])
        return self.vecs[text]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.inner.embed(texts)


def settings_for(data_dir: Path | str, base_url: str | None = None, model: str | None = None) -> Settings:
    s = load_settings(dict(os.environ))
    kw: dict = {"data_dir": Path(data_dir)}
    if base_url:
        kw["llm_base_url"] = base_url
    if model:
        kw["llm_model"] = model
    return dataclasses.replace(s, **kw)


def user_for(space: str) -> User:
    return User(id=f"eval:{space}", name="eval", department=space, spaces=frozenset({space}))


class Harness:
    def __init__(self, settings: Settings, qa: Sequence[QA], llm: LLMClient | None = None,
                 embedder: Embedder | None = None, gold: GoldMap | None = None):
        self.settings, self.qa = settings, list(qa)
        self.gold = gold or GoldMap.load(settings.data_dir)
        self.store = Store(settings.db_path)
        self.audit = AuditLog(settings.data_dir / "eval_audit.db")  # never touches the snapshot's own audit rows
        self.llm = llm
        self.embedder = PrecomputedEmbedder(embedder) if embedder is not None else None
        if self.embedder is not None:
            self.embedder.warm([q.question for q in self.qa])
        self.svc = QueryService(settings, self.store, llm or _NoLLM(), self.audit, embedder=self.embedder)
        self._raw2docs = self.gold.raw_to_docs()
        self._art_docs: dict[str, frozenset[str]] = {}
        self._text: dict[str, str] = {}
        self._foreign: dict[str, list[str]] = {}
        self.ambiguous_leak_facts: list[tuple[str, str]] = []
        self.n_articles = len(self.store.article_paths())
        self.n_embedded = len(self.store.embedding_meta())

    # -- helpers ---------------------------------------------------------------------------------
    def docs_of(self, article: str) -> frozenset[str]:
        if article not in self._art_docs:
            self._art_docs[article] = frozenset(d for r in self.store.article_sources(article)
                                                for d in self._raw2docs.get(r, ()))
        return self._art_docs[article]

    def _article_text(self, path: str) -> str:
        if path not in self._text:
            f = self.settings.wiki_dir / path
            self._text[path] = read_text(f) if f.is_file() else ""
        return self._text[path]

    def foreign_facts(self, space: str) -> list[str]:
        """Facts of OTHER spaces' documents: the space's declared leak_facts plus other spaces' gold/leak facts,
        minus any fact that also occurs in this space's own articles (a fact both spaces legitimately contain
        cannot prove a leak; such declared leak_facts are recorded in `ambiguous_leak_facts`)."""
        if space in self._foreign:
            return self._foreign[space]
        blob = "\n".join(self._article_text(p) for p in self.store.readable_articles({space}))
        out: list[str] = []
        for q in self.qa:
            for f in (*q.leak_facts, *(() if q.space == space else q.gold_facts)):
                if f in out or len(m.normalize(f)) < (1 if q.space == space else 3):
                    continue
                if m.fact_in(f, blob):
                    if q.space == space:
                        self.ambiguous_leak_facts.append((q.id, f))
                    continue
                out.append(f)
        self._foreign[space] = out
        return out

    def leak_reasons(self, q: QA, answer: str, paths: Sequence[str]) -> list[str]:
        spaces = {p: self.store.article_spaces(p) for p in paths}
        return m.detect_leak(q.space, answer, spaces, self.foreign_facts(q.space),
                             {p: self._article_text(p) for p in paths})

    # -- evaluation ------------------------------------------------------------------------------
    def run(self, params: RetrievalParams, mode: str = "retrieval", questions: Sequence[QA] | None = None) -> list[dict]:
        if mode not in ("retrieval", "answer"):
            raise ValueError("mode must be retrieval|answer")
        return [self._one(q, params, mode) for q in (self.qa if questions is None else questions)]

    def _one(self, q: QA, params: RetrievalParams, mode: str) -> dict:
        user, k, gold = user_for(q.space), params.top_k, set(q.gold_docs)
        t0 = time.perf_counter()
        hits = self.svc.retrieve(user, q.question, params)
        ret_ms = (time.perf_counter() - t0) * 1000
        paths = [h.path for h in hits]
        hd = [self.docs_of(p) for p in paths]
        n_rel = len(self.gold.relevant_articles(gold)) or len(gold)
        row: dict = {
            "id": q.id, "type": q.type, "space": q.space, "split": q.split, "hits": paths,
            "empty": not paths, "latency_ms": ret_ms,
            "recall": m.recall_at_k(hd, gold, k), "hit": m.hit_at_k(hd, gold, k), "hit1": m.hit_at_k(hd, gold, 1),
            "mrr": m.mrr(hd, gold), "ndcg": m.ndcg_at_k(hd, gold, k, n_rel),
        }
        if q.stale_docs:
            row["stale_top1"] = bool(hd and set(q.stale_docs) & hd[0] and not gold & hd[0])
        leak_paths = list(paths)
        answer = ""
        if mode == "answer":
            saved0 = getattr(self.llm, "saved_ms", 0.0)
            t0 = time.perf_counter()
            res = self.svc.query(user, q.question, params=params)  # LLM errors propagate: never score a failed call
            row["latency_ms"] = (time.perf_counter() - t0) * 1000 + (getattr(self.llm, "saved_ms", 0.0) - saved0)
            answer = res.answer
            row.update(answer=answer, citations=res.citations, refused=m.is_refusal(answer))
            leak_paths = list(dict.fromkeys(paths + res.citations))
            if q.answerable:
                row["fact_recall"] = m.fact_recall(q.gold_facts, answer)
                row["correct"] = (not row["refused"]) and m.answer_correct(q.gold_facts, answer)
                row["citation_ok"] = m.citation_accurate([self.docs_of(c) for c in res.citations], gold)
            else:
                row["correct"] = row["refused"]
        row["leak"] = self.leak_reasons(q, answer, leak_paths)
        return row

    def close(self) -> None:
        self.store.close()
        self.audit.close()
