"""Shared helpers for engine tests (synthetic data only)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from llmwiki.audit import AuditLog
from llmwiki.auth import User
from llmwiki.config import Settings
from llmwiki.engine.compile import Compiler
from llmwiki.engine.llm import FakeLLM
from llmwiki.engine.query import QueryService
from llmwiki.engine.store import Store
from llmwiki.models import RawRecord

UA = User("ua", "Kim", "dept-a", None, frozenset({"dept-a"}))
UB = User("ub", "Lee", "dept-b", None, frozenset({"dept-b"}))
UAB = User("uab", "Park", "dept-a", None, frozenset({"dept-a", "dept-b"}))


def triage(decision="New", **kw) -> str:
    return json.dumps({"decision": decision, "target": None, "topic": "general", "title": "T",
                       "body": "", "related": [], **kw}, ensure_ascii=False)


@dataclass
class Env:
    settings: Settings
    store: Store
    audit: AuditLog
    scripted: list  # queue of triage replies for the compiler LLM
    compile_llm: FakeLLM
    compiler: Compiler

    def ingest(self, space: str, name: str, text: str, reply: str):
        rec = RawRecord(f"raw/{space}/{name}", space, hashlib.sha256(text.encode()).hexdigest())
        f = self.settings.data_dir / rec.raw_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(text.encode("utf-8"))
        self.scripted.append(reply)
        return rec, self.compiler.compile(rec, text)

    def query_service(self, llm=None) -> QueryService:
        return QueryService(self.settings, self.store, llm or FakeLLM(), self.audit)


def make_env(tmp_path: Path) -> Env:
    settings = Settings("development", tmp_path, "dev", "http://127.0.0.1:8000/v1", "m", False)
    tmp_path.mkdir(exist_ok=True)
    store, audit = Store(settings.db_path), AuditLog(settings.db_path)
    scripted: list = []
    llm = FakeLLM(lambda system, prompt: scripted.pop(0))
    return Env(settings, store, audit, scripted, llm, Compiler(settings, store, llm))
