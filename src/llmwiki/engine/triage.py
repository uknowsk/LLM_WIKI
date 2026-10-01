"""Triage helpers for Compiler: structured-output schema, prompt truncation, LLM call shim and the
deterministic raw-to-article fallback. Pure functions; nothing here touches the wiki or logs text."""
from __future__ import annotations

import inspect
import os
import re

MARKER = "\n\n[... truncated ...]\n\n"
DEFAULT_MAX_CHARS = 12000
CAND_MAX_CHARS = 8000
FALLBACK_BODY_MAX = 60000
RETRY_NOTE = ("\n\nYour previous reply was not valid JSON for the schema; "
              "reply with ONE JSON object only.")
_DIGITS = re.compile(r"\d+")

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["New", "Update", "Disputed", "No material"]},
        "target": {"type": ["string", "null"]},
        "topic": {"type": "string"},
        "title": {"type": "string"},
        "body": {"type": "string"},
        "related": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "target", "topic", "title", "body", "related"],
    "additionalProperties": False,
}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "triage", "strict": True, "schema": TRIAGE_SCHEMA}}


def max_chars() -> int:
    try:
        return max(1000, int(os.environ.get("WIKI_COMPILE_MAX_CHARS", DEFAULT_MAX_CHARS)))
    except ValueError:
        return DEFAULT_MAX_CHARS


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Keep head and tail with a visible marker. Returns (text, was_truncated)."""
    if len(text) <= limit:
        return text, False
    head = (limit * 2) // 3
    return text[:head] + MARKER + text[len(text) - (limit - head):], True


def has_data(text: str) -> bool:
    """Numbers/dates/tables: >= 3 digit groups. A wiki must not silently drop such documents."""
    return len(_DIGITS.findall(text)) >= 3


def call_llm(llm, system: str, prompt: str, temperature: float | None = None, response_format: dict | None = None) -> str:
    """Call llm.complete (or a plain callable) passing optional args only if its signature accepts them."""
    fn = getattr(llm, "complete", None) or llm
    kw: dict = {}
    try:
        params = inspect.signature(fn).parameters
        var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        params, var_kw = {}, False
    if temperature is not None:
        kw["temperature"] = temperature
    if response_format is not None and ("response_format" in params or var_kw):
        kw["response_format"] = response_format
    return fn(system, prompt, **kw)


def raw_fallback(text: str, raw_path: str) -> tuple[str, str]:
    """(title, body) for a deterministic New article: title from the first heading/title line or the file
    stem; body is the raw text without the leading title line and the '> Source/Collected/Published' block."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    title = ""
    if i < len(lines) and lines[i].lstrip().startswith("#"):
        title = lines[i].lstrip().lstrip("#").strip()
        i += 1
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith(">")):
        i += 1
    body = "\n".join(lines[i:]).strip()
    if not title:
        stem = raw_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        title = stem or "untitled"
    if len(body) > FALLBACK_BODY_MAX:
        body = body[:FALLBACK_BODY_MAX].rstrip() + "\n\n[... truncated ...]"
    return title, body
