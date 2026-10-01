"""Query and article read API. The ACL filter runs BEFORE ranking and before any LLM context is built."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..acl import can_read
from ..audit import AuditLog
from ..auth import User
from ..config import Settings
from . import article as art
from .fsutil import read_text
from .embed import Embedder
from .llm import LLMClient
from .params import RetrievalParams, RetrievedHit
from .retrieval import Retrieval
from .store import Store

NO_EVIDENCE = "근거 없음"
SYSTEM = (
    "Answer the question using ONLY the numbered context articles. Cite them as [n]. "
    f"If the context does not contain the answer, reply exactly: {NO_EVIDENCE}"
)
_MARKER = re.compile(r"\[(\d+)\]")


class AccessDenied(Exception):
    """Raised for unreadable AND nonexistent articles alike, so existence is not revealed."""


@dataclass(frozen=True)
class QueryResult:
    answer: str
    citations: list[str]  # wiki-relative article paths, all readable by the asking user


class QueryService:
    def __init__(self, settings: Settings, store: Store, llm: LLMClient, audit: AuditLog,
                 embedder: Embedder | None = None, params: RetrievalParams | None = None):
        self.settings, self.store, self.llm, self.audit = settings, store, llm, audit
        self.embedder, self.params = embedder, params
        self._retrieval = Retrieval(store, settings.wiki_dir)

    def retrieve(self, user: User, question: str, params: RetrievalParams | None = None) -> list[RetrievedHit]:
        """Ranked, ACL-filtered hits (no LLM call, no audit record). The ACL runs before any ranking.
        NOTE: this does not audit. If it is ever exposed to end users (API/UI), the caller MUST record
        an audit entry for `user` first; it is an evaluation/internal hook."""
        return self._retrieval.rank(user.spaces, question, params or self.params or RetrievalParams(), self.embedder)

    def query(self, user: User, question: str, k: int = 5, params: RetrievalParams | None = None) -> QueryResult:
        p = params or self.params or RetrievalParams(top_k=k)  # explicit params win over the legacy `k`
        # filter first: unreadable text never enters ranking, statistics or the prompt
        docs: dict[str, str] = {}
        for h in self.retrieve(user, question, p):
            f = self.settings.wiki_dir / h.path  # only the top-k files are read, lazily
            if f.is_file():
                docs[h.path] = read_text(f)
        hits = list(docs)
        if not hits:
            self.audit.record(user, "query", question, "no-evidence")
            return QueryResult(NO_EVIDENCE, [])
        cap = p.max_context_chars or None
        context = "\n\n".join(f"[{i}] {h}\n{art.split_body(docs[h])[:cap]}" for i, h in enumerate(hits, 1))
        # intent is logged BEFORE the LLM call so a failed/aborted call still leaves a trace
        self.audit.record(user, "query_intent", question, "context: " + ", ".join(hits))
        try:
            kw = {} if p.temperature is None else {"temperature": p.temperature}  # custom clients may lack the kwarg
            answer = self.llm.complete(SYSTEM, f"CONTEXT:\n{context}\n\nQUESTION: {question}", **kw).strip()
        except Exception as e:
            self.audit.record(user, "query", question, f"llm-error: {type(e).__name__}")
            raise
        if answer == NO_EVIDENCE:
            self.audit.record(user, "query", question, "no-evidence")
            return QueryResult(NO_EVIDENCE, [])
        used = [int(n) for n in _MARKER.findall(answer)]
        if used:  # cite only what the answer actually references; drop markers that point nowhere
            valid = sorted({n for n in used if 1 <= n <= len(hits)})
            answer = _MARKER.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(hits) else "", answer)
            cited = [hits[n - 1] for n in valid]
            detail = ("cited: " + ", ".join(cited)) if cited else "invalid-citations"
            if len(valid) != len(set(used)):
                detail += " (invalid markers dropped)"
        else:
            cited, detail = hits, "cited: " + ", ".join(hits)
        self.audit.record(user, "query", question, detail)
        return QueryResult(answer, cited)

    def read_article(self, user: User, path: str) -> str:
        """Server-side re-check for citation clicks. Same error for denied and missing."""
        ok, f = False, None
        try:
            if isinstance(path, str) and path in self.store.article_paths():
                wiki = self.settings.wiki_dir.resolve()
                f = (wiki / path).resolve()
                ok = (
                    f.is_relative_to(wiki)
                    and f.suffix == ".md"
                    and f.is_file()
                    and can_read(user, self.store.article_spaces(path))
                )
        except (OSError, ValueError):  # NUL bytes, over-long or invalid paths
            ok = False
        if not ok or f is None:
            self.audit.record(user, "read_denied", path if isinstance(path, str) else repr(path))
            raise AccessDenied(path)
        self.audit.record(user, "read", path)
        return read_text(f)
