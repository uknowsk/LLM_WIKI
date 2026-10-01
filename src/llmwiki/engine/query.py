"""Query and article read API. The ACL filter runs BEFORE ranking and before any LLM context is built."""
from __future__ import annotations

from dataclasses import dataclass

from ..acl import can_read
from ..audit import AuditLog
from ..auth import User
from ..config import Settings
from . import article as art
from .llm import LLMClient
from .search import bm25_rank
from .store import Store

NO_EVIDENCE = "근거 없음"
SYSTEM = (
    "Answer the question using ONLY the numbered context articles. Cite them as [n]. "
    f"If the context does not contain the answer, reply exactly: {NO_EVIDENCE}"
)


class AccessDenied(Exception):
    """Raised for unreadable AND nonexistent articles alike, so existence is not revealed."""


@dataclass(frozen=True)
class QueryResult:
    answer: str
    citations: list[str]  # wiki-relative article paths, all readable by the asking user


class QueryService:
    def __init__(self, settings: Settings, store: Store, llm: LLMClient, audit: AuditLog):
        self.settings, self.store, self.llm, self.audit = settings, store, llm, audit

    def _readable(self, user: User) -> dict[str, str]:
        docs: dict[str, str] = {}
        for p in self.store.article_paths():
            if not can_read(user, self.store.article_spaces(p)):
                continue
            f = self.settings.wiki_dir / p
            if f.is_file():
                docs[p] = f.read_text(encoding="utf-8")
        return docs

    def query(self, user: User, question: str, k: int = 5) -> QueryResult:
        docs = self._readable(user)  # filter first: unreadable text never enters ranking or prompt
        hits = [p for p, _ in bm25_rank(question, docs)[:k]]
        if not hits:
            self.audit.record(user, "query", question, "no-evidence")
            return QueryResult(NO_EVIDENCE, [])
        context = "\n\n".join(f"[{i}] {p}\n{art.split_body(docs[p])}" for i, p in enumerate(hits, 1))
        answer = self.llm.complete(SYSTEM, f"CONTEXT:\n{context}\n\nQUESTION: {question}").strip()
        if answer == NO_EVIDENCE:
            self.audit.record(user, "query", question, "no-evidence")
            return QueryResult(NO_EVIDENCE, [])
        self.audit.record(user, "query", question, "cited: " + ", ".join(hits))
        return QueryResult(answer, hits)

    def read_article(self, user: User, path: str) -> str:
        """Server-side re-check for citation clicks. Same error for denied and missing."""
        wiki = self.settings.wiki_dir.resolve()
        f = (wiki / path).resolve()
        ok = (
            f.is_relative_to(wiki)
            and f.suffix == ".md"
            and f.is_file()
            and path in self.store.article_paths()
            and can_read(user, self.store.article_spaces(path))
        )
        if not ok:
            self.audit.record(user, "read_denied", path)
            raise AccessDenied(path)
        self.audit.record(user, "read", path)
        return f.read_text(encoding="utf-8")
