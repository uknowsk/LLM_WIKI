"""Shared helpers for pipeline tests (synthetic data only)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from llmwiki.audit import AuditLog
from llmwiki.auth import User
from llmwiki.config import Settings
from llmwiki.engine.llm import FakeLLM
from llmwiki.engine.store import Store
from llmwiki.pipeline.queue import JobQueue
from llmwiki.pipeline.run import process_file
from llmwiki.pipeline.watch import Watcher

UA = User("ua", "Kim", "dept-a", None, frozenset({"dept-a"}))


def responder(system: str, prompt: str) -> str:
    """Deterministic triage: always New, titled after the raw H1, body = raw text."""
    raw = prompt.split("\n\nCANDIDATE ARTICLES:")[0].split("):\n", 1)[1]
    if "BADJSON" in raw:
        return "this is not json"
    title = re.match(r"# (.+)", raw).group(1)
    body = raw.split("\n\n", 2)[2] if raw.count("\n\n") >= 2 else raw
    return json.dumps({"decision": "New", "target": None, "topic": "general", "title": title,
                       "body": body, "related": []}, ensure_ascii=False)


@dataclass
class Env:
    settings: Settings
    store: Store
    audit: AuditLog
    llm: FakeLLM
    queue: JobQueue
    watcher: Watcher

    def drop(self, space: str, name: str, data: bytes | str) -> Path:
        base = self.settings.data_dir / "inbox"
        p = base / space / name if space else base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return p

    def process(self, path: Path, space: str, **kw):
        return process_file(path, space, self.settings, self.llm, self.store, self.audit, **kw)

    def run_all(self, **kw):
        self.watcher.scan_once()
        return self.queue.drain(lambda p, s: self.process(p, s, **kw))

    def system_actions(self) -> list[str]:
        return [a for _, u, a, _, _ in self.audit.entries() if u == "system:pipeline"]


def make_env(tmp_path: Path, mask: bool = False, llm: FakeLLM | None = None) -> Env:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings("development", data, "dev", "http://127.0.0.1:8000/v1", "m", mask)
    store, audit = Store(settings.db_path), AuditLog(settings.db_path)
    queue = JobQueue(settings)
    return Env(settings, store, audit, llm or FakeLLM(responder), queue, Watcher(settings, queue, audit))


def make_eml(subject: str, body: str, attachments: list[tuple[str, bytes]] = ()) -> bytes:
    m = EmailMessage()
    m["From"], m["To"] = "a@example.com", "b@example.com"
    m["Subject"], m["Date"] = subject, "Tue, 30 Sep 2026 09:00:00 +0900"
    m["Message-ID"] = f"<{abs(hash(subject))}@example.com>"
    m.set_content(body)
    for name, payload in attachments:
        m.add_attachment(payload, maintype="text", subtype="plain", filename=name)
    return m.as_bytes()
