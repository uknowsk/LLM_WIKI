"""ACL-first hybrid retrieval (BM25 + dense cosine, fused by reciprocal-rank fusion).

Order of operations, always: (1) the readable article set for the user's spaces is computed from the DB
in one query and cached per (frozenset(spaces), store.version); (2) ONLY THEN do ranking structures get
touched. Unreadable articles are never read from disk, never tokenized, never part of idf/avg-length,
never scored, never a tie-break. Per-article token counts / vectors are cached per process, keyed by the
article revision / embedded-text hash, and loaded lazily for readable articles only.
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from array import array
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .embed import Embedder, cosine, normalize, unpack
from .fsutil import read_text
from .params import RetrievalParams, RetrievedHit
from .search import bm25_scores, tokenize
from .store import Store

log = logging.getLogger("llmwiki.retrieval")
_MAX_SNAPSHOTS = 256
_QCACHE = 256
EMBED_COOLDOWN = 30.0  # seconds to skip dense retrieval after an embedding failure


@dataclass(frozen=True)
class Snapshot:
    readable: dict[str, int]  # path -> revision (ACL-filtered)
    meta: dict[str, tuple[str, int, str]]  # readable path -> (model, dim, text_sha256) of its embedding


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.version: tuple[int, int] | None = None
        self.snaps: dict[frozenset[str], Snapshot] = {}
        self.tokens: dict[str, tuple[int, Counter]] = {}
        self.vecs: dict[str, tuple[tuple[str, int, str], array]] = {}  # path -> ((model, dim, sha), vector)
        self.qcache: OrderedDict[str, array] = OrderedDict()  # sha256(model+question) -> vector (memory only)
        self.embed_down_until = 0.0


class Retrieval:
    def __init__(self, store: Store, wiki_dir: Path, clock: Callable[[], float] = time.monotonic):
        self.store, self.wiki_dir, self.clock = store, Path(wiki_dir), clock
        self._st: _State = store.cache.setdefault("retrieval", _State())  # shared by every QueryService on this store

    # -- step 1: the readable set --------------------------------------------------------------
    def snapshot(self, spaces: Iterable[str]) -> Snapshot:
        key, st = frozenset(spaces), self._st
        ver = self.store.version  # read BEFORE the queries: a concurrent write can only make us recompute
        with st.lock:
            if ver != st.version:  # SQLite data_version is not monotonic: any change resets
                st.snaps.clear()
                st.version = ver
                live = set(self.store.article_paths())  # purge caches of deleted articles
                for d in (st.tokens, st.vecs):
                    for p in [p for p in d if p not in live]:
                        del d[p]
            hit = st.snaps.get(key) if ver == st.version else None
        if hit is not None:
            return hit
        readable = self.store.readable_articles(key)
        snap = Snapshot(readable, self.store.embedding_meta(readable))
        with st.lock:
            if ver == st.version:  # never store a snapshot older than the cache generation
                if len(st.snaps) >= _MAX_SNAPSHOTS:
                    st.snaps.clear()
                st.snaps[key] = snap
        return snap

    # -- step 2: per-article data, only for readable paths -------------------------------------
    def _tokens(self, readable: dict[str, int]) -> dict[str, Counter]:
        st, out = self._st, {}
        for p, rev in readable.items():
            with st.lock:
                ent = st.tokens.get(p)
            if ent is None or ent[0] != rev:
                f = self.wiki_dir / p
                if not f.is_file():
                    continue  # no file => not a document (same as the old behavior)
                ent = (rev, Counter(tokenize(read_text(f))))
                with st.lock:
                    st.tokens[p] = ent
            out[p] = ent[1]
        return out

    def _vectors(self, snap: Snapshot, model: str | None, dim: int) -> dict[str, array]:
        st = self._st
        want = {p: m for p, m in snap.meta.items() if m[1] == dim and (model is None or m[0] == model)}
        with st.lock:
            missing = [p for p, m in want.items() if (st.vecs.get(p) or (None, None))[0] != m]
        if missing:
            blobs = self.store.embedding_vectors(missing)
            with st.lock:
                for p, blob in blobs.items():
                    v = unpack(blob) if len(blob) == dim * 4 else None
                    n = math.sqrt(sum(x * x for x in v)) if v is not None else 0.0
                    if v is not None and math.isfinite(n) and abs(n - 1.0) < 0.01:
                        st.vecs[p] = (want[p], v)
                    else:  # tampered/corrupt row: this article is BM25-only
                        st.vecs.pop(p, None)
                        log.warning("ignoring invalid stored vector for %s", p)
        with st.lock:
            return {p: st.vecs[p][1] for p, m in want.items() if p in st.vecs and st.vecs[p][0] == m}

    def _query_vector(self, question: str, embedder: Embedder) -> array | None:
        """Cached, short-timeout, cooldown-guarded query embedding. None => dense unavailable (BM25 only)."""
        st = self._st
        key = hashlib.sha256((getattr(embedder, "model", "") + "\0" + question).encode("utf-8")).hexdigest()
        with st.lock:
            if key in st.qcache:
                st.qcache.move_to_end(key)
                return st.qcache[key]
            if self.clock() < st.embed_down_until:
                return None
        try:
            fn = getattr(embedder, "embed_query", None)
            qv = normalize(fn(question) if fn else embedder.embed([question])[0])
        except Exception as e:  # EmbedError, network, bad shape: dense is optional, fall back to BM25
            with st.lock:
                st.embed_down_until = self.clock() + EMBED_COOLDOWN
            log.warning("query embedding failed (%s); dense skipped for %ds", type(e).__name__, EMBED_COOLDOWN)
            return None
        with st.lock:
            st.qcache[key] = qv
            while len(st.qcache) > _QCACHE:
                st.qcache.popitem(last=False)
        return qv

    def _dense(self, snap: Snapshot, question: str, embedder: Embedder, params: RetrievalParams) -> list[tuple[str, float]]:
        if not snap.meta:
            return []
        qv = self._query_vector(question, embedder)
        if qv is None:
            return []
        vecs = self._vectors(snap, getattr(embedder, "model", None), len(qv))
        scored = [(p, cosine(qv, v)) for p, v in vecs.items()]
        scored = [(p, s) for p, s in scored if s > params.min_cosine]
        return sorted(scored, key=lambda x: (-x[1], x[0]))[:params.candidate_pool]

    # -- ranking -------------------------------------------------------------------------------
    def rank(self, spaces: Iterable[str], question: str, params: RetrievalParams,
             embedder: Embedder | None = None) -> list[RetrievedHit]:
        snap = self.snapshot(spaces)  # ACL first; nothing below can see an unreadable article
        if not snap.readable:
            return []
        mode = params.retrieval_mode
        bm: list[tuple[str, float]] = []
        dn: list[tuple[str, float]] = []
        if mode in ("bm25", "hybrid"):
            bm = bm25_scores(tokenize(question), self._tokens(snap.readable), params.bm25_k1, params.bm25_b)
            bm = bm[:params.candidate_pool]
        if mode in ("dense", "hybrid") and embedder is not None:
            dn = self._dense(snap, question, embedder, params)
        if mode == "bm25":
            fused = bm
        elif mode == "dense":
            fused = dn
        else:
            acc: dict[str, float] = {}
            for w, lst in ((params.w_bm25, bm), (params.w_dense, dn)):
                for r, (p, _) in enumerate(lst, 1):
                    acc[p] = acc.get(p, 0.0) + w / (params.rrf_k + r)
            fused = sorted(acc.items(), key=lambda x: (-x[1], x[0]))
        return [RetrievedHit(p, s, i) for i, (p, s) in enumerate(fused[:params.top_k], 1)]
