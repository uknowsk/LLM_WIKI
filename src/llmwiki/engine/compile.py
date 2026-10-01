"""Compile a raw document into the wiki: triage -> write/merge -> index/log -> cascade.

Space isolation: only articles whose source-space set EQUALS {record.space} are ever shown to the
LLM as candidates or modified, so compilation can never merge content across spaces. (A mixed-space
article, once it exists, is therefore never touched by compilation.)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Settings
from ..models import RawRecord
from . import article as art
from . import lint
from . import triage as tg
from .embed import Embedder, embed_article
from .fsutil import append_line, atomic_write, read_text
from .llm import LLMClient
from .search import bm25_rank
from .store import Store

log = logging.getLogger("llmwiki.compile")
DECISIONS = ("New", "Update", "Disputed", "No material")
MAX_CANDIDATES = 5
MIN_UPDATE_RATIO = 0.5  # an Update shorter than this fraction of the old body is refused
_THINK = re.compile(r"\A\s*<think>.*?</think>", re.S | re.I)
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

SYSTEM = (
    "You maintain a company wiki. Use ONLY facts in the RAW text. Copy every number, date and quote "
    "verbatim from RAW. RAW begins with a metadata header (a title line and '> Source/Collected/Published' "
    "lines): never copy that header or repeat the title inside body. Reply with one JSON object: "
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
    fallback: str | None = None  # reason when the deterministic raw->New fallback was used


def _space_slug(space: str) -> str:
    return art.slugify(space.replace("/", "__"), "space")


def _parse_json(reply: str) -> dict:
    """First balanced JSON object in the reply (after an optional leading <think> block), type-validated."""
    text = reply if isinstance(reply, str) else ""
    while (m := _THINK.match(text)):
        text = text[m.end():]
    dec, data, i = json.JSONDecoder(), None, text.find("{")
    while i != -1 and data is None:
        try:
            obj, _ = dec.raw_decode(text, i)
            data = obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
        i = text.find("{", i + 1)
    if not isinstance(data, dict) or data.get("decision") not in DECISIONS:
        raise CompileError("LLM triage reply is not valid JSON with a known decision")
    related = data.get("related")
    if data.get("target") is not None and not isinstance(data["target"], str):
        raise CompileError("LLM triage reply: 'target' must be a string")
    if related is not None and not (isinstance(related, list) and all(isinstance(r, str) for r in related)):
        raise CompileError("LLM triage reply: 'related' must be a list of strings")
    for k in ("topic", "title", "body"):
        if data.get(k) is not None and not isinstance(data[k], str):
            raise CompileError(f"LLM triage reply: {k!r} must be a string")
    return data


class Compiler:
    def __init__(self, settings: Settings, store: Store, llm: LLMClient, embedder: Embedder | None = None):
        self.settings, self.store, self.llm, self.embedder = settings, store, llm, embedder
        self._lock = threading.RLock()  # compile() is serialized: one writer for the wiki tree

    # -- helpers -----------------------------------------------------------
    def _read(self, path: str) -> art.Article:
        a = art.parse(path, read_text(self.settings.wiki_dir / path))
        a.sources = self.store.article_sources(path)  # source of truth is the DB, not the file
        return a

    def _write(self, a: art.Article, space: str) -> None:
        """Validate, back up the previous version, write atomically, then register in the store."""
        if any(self.store.raw_space(s) != space for s in a.sources):
            raise CompileError("article source is not a registered raw file of this space")
        spaces = frozenset({space})
        # related only ever points at existing articles of exactly the same space set
        a.related = [r for r in dict.fromkeys(a.related) if r != a.path and self.store.article_spaces(r) == spaces]
        a.updated = datetime.now(timezone.utc).date().isoformat()
        titles = {r: self.store.article_title(r) or r for r in a.related}
        f = self.settings.wiki_dir / a.path
        if f.is_file():
            shutil.copyfile(f, f.with_name(f.name + ".bak"))  # previous version, outside the index
        rendered = art.render(a, titles)
        atomic_write(f, rendered)
        self.store.upsert_article(a.path, a.title, a.sources, space=space)
        self._embed(a.path, rendered)

    def _embed(self, path: str, rendered: str) -> None:
        """Best effort: an embedder outage must never fail a compile. On failure the article is left
        unembedded (stale vector removed) and queries fall back to BM25 for it. Text is never logged."""
        if self.embedder is not None and embed_article(self.store, self.embedder, path, rendered) == "failed":
            log.warning("embedding failed for %s; article left unembedded (BM25 fallback)", path)

    def _candidates(self, rec: RawRecord, text: str) -> dict[str, art.Article]:
        same = self.store.articles_with_spaces(frozenset({rec.space}))
        arts = {p: self._read(p) for p in same if (self.settings.wiki_dir / p).is_file()}
        ranked = bm25_rank(text, {p: f"{a.title}\n{a.body}" for p, a in arts.items()})
        return {p: arts[p] for p, _ in ranked[:MAX_CANDIDATES]}

    def _new_path(self, rec: RawRecord, topic: str, title: str) -> str:
        base = f"{art.slugify(topic, 'general')}/{art.slugify(title)}"
        taken = set(self.store.article_paths())

        def free(p: str) -> bool:  # also check the filesystem: never overwrite an orphan file
            return p not in taken and not (self.settings.wiki_dir / p).exists()

        path = f"{base}.md"
        if free(path):
            return path
        path = f"{base}-{_space_slug(rec.space)}.md"  # never collide with another space's article
        i = 2
        while not free(path):
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
        append_line(self._meta_dir(rec.space) / "log.md", line)

    def _update_index(self, space: str) -> None:
        lines = ["# Index", ""]
        for p in self.store.articles_with_spaces(frozenset({space})):
            lines.append(f"- [[{art.link_target(p)}|{art.label(self.store.article_title(p) or p)}]]")
        atomic_write(self._meta_dir(space) / "index.md", "\n".join(lines) + "\n")

    def _check_raw(self, rec: RawRecord) -> None:
        parts = rec.raw_path.split("/")
        if (not rec.raw_path.startswith(f"raw/{rec.space}/") or ".." in parts or "" in parts
                or _CTRL.search(rec.raw_path) or "\\" in rec.raw_path):
            raise CompileError(f"raw path {rec.raw_path!r} is not under raw/{rec.space}/")
        f = self.settings.data_dir / rec.raw_path
        if f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() != rec.sha256:
            raise CompileError(f"raw file {rec.raw_path!r} does not match its recorded sha256")

    # -- main entry --------------------------------------------------------
    def compile(self, rec: RawRecord, text: str) -> CompileResult:
        with self._lock:
            return self._compile(rec, text)

    def _compile(self, rec: RawRecord, text: str) -> CompileResult:
        if not rec.space:
            raise CompileError("raw record has no space (fail closed)")
        self._check_raw(rec)
        try:
            self.store.add_raw(rec)
        except ValueError as e:
            raise CompileError(str(e)) from e
        cands = self._candidates(rec, text)
        shown, _ = tg.truncate(text, tg.max_chars())
        cand_txt, cut = [], set()
        for p, a in cands.items():
            body, was_cut = tg.truncate(a.body, tg.CAND_MAX_CHARS)
            if was_cut:
                cut.add(p)
            cand_txt.append(f"### {p}\ntitle: {a.title}\n{body}")
        prompt = "RAW (" + rec.raw_path + "):\n" + shown + "\n\nCANDIDATE ARTICLES:\n" + "\n".join(cand_txt)
        data, reason = self._triage(prompt)
        if data is not None:
            decision, target = data["decision"], data.get("target")
            if decision in ("Update", "Disputed") and target not in cands:
                reason = "bad-target"
            elif decision == "No material" and tg.has_data(text):
                reason = "no-material-with-data"
            elif decision == "Update" and target in cut:
                reason = "candidate-truncated"
        if reason is None:
            if decision == "No material":
                self._log(rec, decision, None)
                return CompileResult(decision, None)
            body = str(data.get("body") or "").strip()
            title = art.clean_title(data.get("title") or "")
            if decision == "Update" and not body:
                reason = "empty-body"
            elif decision == "Update" and not title:
                reason = "empty-title"
            elif decision == "Update" and len(body) < MIN_UPDATE_RATIO * len(cands[target].body):
                reason = "shrink"
            elif not body:
                raise CompileError("LLM returned an empty body")
            elif decision == "New" and not title:
                raise CompileError("LLM returned an empty title")
        if reason is not None:
            return self._fallback_new(rec, text, reason)

        if decision == "New":
            a = art.Article(self._new_path(rec, str(data.get("topic") or "general"), title), title, body, [rec.raw_path])
        else:
            a = cands[target]
            if rec.raw_path not in a.sources:
                a.sources.append(rec.raw_path)
            if decision == "Update":
                a.body = body
            else:
                a.body = f"{a.body}\n\n## Disputed\n{body}\n(Source: {rec.raw_path})"

        a.related = list(dict.fromkeys(a.related + [r for r in data.get("related") or [] if r in cands and r != a.path]))
        self._write(a, rec.space)
        self._cascade(a, cands, rec.space)
        return self._finish(rec, decision, a, None)

    def _finish(self, rec: RawRecord, decision: str, a: art.Article, fallback: str | None) -> CompileResult:
        self._update_index(rec.space)
        n = len(lint.lint_article(self.settings, self.store, a.path)) if (self.settings.data_dir / rec.raw_path).is_file() else 0
        notes = ([f"fallback:{fallback}"] if fallback else []) + ([f"(grounding suspects: {n})"] if n else [])
        self._log(rec, decision, a.path, " ".join(notes))
        return CompileResult(decision, a.path, n, fallback)

    def _triage(self, prompt: str) -> tuple[dict | None, str | None]:
        """Ask the model (at most 2 attempts). Returns (data, None) or (None, fallback reason)."""
        for attempt in (0, 1):
            try:
                if attempt == 0:
                    reply = tg.call_llm(self.llm, SYSTEM, prompt, response_format=tg.RESPONSE_FORMAT)
                else:
                    reply = tg.call_llm(self.llm, SYSTEM, prompt + tg.RETRY_NOTE, 0.0, tg.RESPONSE_FORMAT)
                return _parse_json(reply), None
            except CompileError:
                continue
        return None, "invalid-reply"

    def _fallback_new(self, rec: RawRecord, text: str, reason: str) -> CompileResult:
        """Deterministic New article from the raw text. Never merges into or modifies any other article."""
        title, body = tg.raw_fallback(text, rec.raw_path)
        title = art.clean_title(title) or "untitled"
        if not body:
            raise CompileError("raw document has no content to compile")
        a = art.Article(self._new_path(rec, "general", title), title, body, [rec.raw_path])
        self._write(a, rec.space)
        log.warning("compile fallback (%s) for %s", reason, rec.raw_path)
        return self._finish(rec, "New", a, reason)

    def _cascade(self, a: art.Article, cands: dict[str, art.Article], space: str) -> None:
        """Back-link related same-space articles so the new/updated knowledge is reachable from them."""
        for r in a.related:
            other = cands.get(r)
            if other is None:  # not a candidate this round; its own back-link already exists or is skipped
                continue
            if a.path not in other.related:
                other.related.append(a.path)
                self._write(other, space)
