"""Compile a raw document into the wiki: triage -> write/merge -> index/log -> cascade.

Space isolation: only articles whose source-space set EQUALS {record.space} are ever shown to the
LLM as candidates or modified, so compilation can never merge content across spaces. (A mixed-space
article, once it exists, is therefore never touched by compilation.)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Settings
from ..models import RawRecord
from . import article as art
from . import lint
from .llm import LLMClient
from .search import bm25_rank
from .store import Store

DECISIONS = ("New", "Update", "Disputed", "No material")
MAX_CANDIDATES = 5

SYSTEM = (
    "You maintain a company wiki. Use ONLY facts in the RAW text. Copy every number, date and quote "
    "verbatim from RAW. Reply with one JSON object: "
    '{"decision": "New|Update|Disputed|No material", "target": "<candidate path or null>", '
    '"topic": "...", "title": "...", "body": "<markdown>", "related": ["<candidate path>"]}. '
    "New: create an article. Update: body is the full merged article for target. "
    "Disputed: RAW conflicts with target; body is only the conflict note. No material: nothing worth keeping."
)


class CompileError(Exception):
    pass


@dataclass(frozen=True)
class CompileResult:
    decision: str
    article: str | None
    suspects: int = 0


def _space_slug(space: str) -> str:
    return art.slugify(space.replace("/", "__"), "space")


def _parse_json(reply: str) -> dict:
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or data.get("decision") not in DECISIONS:
        raise CompileError("LLM triage reply is not valid JSON with a known decision")
    return data


class Compiler:
    def __init__(self, settings: Settings, store: Store, llm: LLMClient):
        self.settings, self.store, self.llm = settings, store, llm

    # -- helpers -----------------------------------------------------------
    def _read(self, path: str) -> art.Article:
        return art.parse(path, (self.settings.wiki_dir / path).read_text(encoding="utf-8"))

    def _write(self, a: art.Article) -> None:
        a.updated = datetime.now(timezone.utc).date().isoformat()
        titles = {r: self.store.article_title(r) or r for r in a.related}
        f = self.settings.wiki_dir / a.path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(art.render(a, titles), encoding="utf-8")
        self.store.upsert_article(a.path, a.title, a.sources)

    def _candidates(self, rec: RawRecord, text: str) -> dict[str, art.Article]:
        same = self.store.articles_with_spaces(frozenset({rec.space}))
        arts = {p: self._read(p) for p in same if (self.settings.wiki_dir / p).is_file()}
        ranked = bm25_rank(text, {p: f"{a.title}\n{a.body}" for p, a in arts.items()})
        return {p: arts[p] for p, _ in ranked[:MAX_CANDIDATES]}

    def _new_path(self, rec: RawRecord, topic: str, title: str) -> str:
        base = f"{art.slugify(topic, 'general')}/{art.slugify(title)}"
        path = f"{base}.md"
        if path in self.store.article_paths() or (self.settings.wiki_dir / path).exists():
            path = f"{base}-{_space_slug(rec.space)}.md"  # never collide with another space's article
        i = 2
        while path in self.store.article_paths():
            path = f"{base}-{_space_slug(rec.space)}-{i}.md"
            i += 1
        return path

    def _meta_dir(self, space: str):
        d = self.settings.wiki_dir / "_meta" / _space_slug(space)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _log(self, rec: RawRecord, decision: str, target: str | None, note: str = "") -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        line = f"## [{day}] ingest | {decision} | {rec.raw_path} -> {target or '-'} {note}".rstrip()
        with (self._meta_dir(rec.space) / "log.md").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _update_index(self, space: str) -> None:
        lines = ["# Index", ""]
        for p in self.store.articles_with_spaces(frozenset({space})):
            lines.append(f"- [[{art.link_target(p)}|{self.store.article_title(p) or p}]]")
        (self._meta_dir(space) / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -- main entry --------------------------------------------------------
    def compile(self, rec: RawRecord, text: str) -> CompileResult:
        if not rec.space:
            raise CompileError("raw record has no space (fail closed)")
        self.store.add_raw(rec)
        cands = self._candidates(rec, text)
        prompt = "RAW (" + rec.raw_path + "):\n" + text + "\n\nCANDIDATE ARTICLES:\n" + "\n".join(
            f"### {p}\ntitle: {a.title}\n{a.body}" for p, a in cands.items()
        )
        data = _parse_json(self.llm.complete(SYSTEM, prompt))
        decision = data["decision"]
        target = data.get("target")
        if decision in ("Update", "Disputed") and target not in cands:
            raise CompileError(f"triage target {target!r} is not a same-space candidate")

        if decision == "No material":
            self._log(rec, decision, None)
            return CompileResult(decision, None)

        body = str(data.get("body", "")).strip()
        if decision == "New":
            title = str(data.get("title") or rec.raw_path)
            a = art.Article(self._new_path(rec, str(data.get("topic") or "general"), title), title, body, [rec.raw_path])
        else:
            a = cands[target]
            if rec.raw_path not in a.sources:
                a.sources.append(rec.raw_path)
            a.body = body if decision == "Update" else f"{a.body}\n\n## Disputed\n{body}\n(Source: {rec.raw_path})"

        related = [r for r in data.get("related") or [] if r in cands and r != a.path]
        a.related = list(dict.fromkeys(a.related + related))
        self._write(a)
        self._cascade(a, cands)
        self._update_index(rec.space)
        n = len(lint.lint_article(self.settings, self.store, a.path)) if (self.settings.data_dir / rec.raw_path).is_file() else 0
        self._log(rec, decision, a.path, f"(grounding suspects: {n})" if n else "")
        return CompileResult(decision, a.path, n)

    def _cascade(self, a: art.Article, cands: dict[str, art.Article]) -> None:
        """Back-link related same-space articles so the new/updated knowledge is reachable from them."""
        for r in a.related:
            other = cands.get(r)
            if other is None:  # not a candidate this round; its own back-link already exists or is skipped
                continue
            if a.path not in other.related:
                other.related.append(a.path)
                self._write(other)
