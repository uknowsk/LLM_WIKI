"""Article markdown format (Obsidian-compatible). Sources/Related sections always come last.

Trust model: the frontmatter is written only by `render` (JSON-encoded values, one line each) and is
the only place `sources`/`related` are parsed from. Body text (LLM output) is never re-parsed as
structure: `render` escapes any "## Sources" / "## Related" heading inside the body.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field

_SRC_HDR = "\n## Sources"
_BODY_HDR = re.compile(r"(?im)^([ \t]*)(##)([ \t]*)(Sources|Related)")


@dataclass
class Article:
    path: str  # wiki-relative, e.g. "topic/slug.md"
    title: str
    body: str
    sources: list[str] = field(default_factory=list)  # raw paths, data-dir-relative
    related: list[str] = field(default_factory=list)  # wiki-relative article paths (.md)
    updated: str = ""


def slugify(text: str, default: str = "untitled") -> str:
    s = re.sub(r"[^\w-]+", "-", text.strip().lower(), flags=re.UNICODE).strip("-_")
    return s[:80] or default


def link_target(path: str) -> str:
    return path[:-3] if path.endswith(".md") else path


def clean_title(title: str) -> str:
    """One line, no control/format characters (newlines, U+2028, ...), bounded length."""
    t = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in str(title))
    return re.sub(r"\s+", " ", t).strip()[:200]


def _escape_body(body: str) -> str:
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    return _BODY_HDR.sub(lambda m: m.group(1) + "\\" + m.group(2) + m.group(3) + m.group(4), body)


def label(s: str) -> str:
    return re.sub(r"[\[\]|]", " ", clean_title(s))


def render(a: Article, titles: dict[str, str]) -> str:
    rel_to_root = "../" * (a.path.count("/") + 1)  # article lives under <data>/wiki/
    title = clean_title(a.title)
    j = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731
    lines = ["---", f"title: {j(title)}", f"updated: {j(a.updated)}", f"sources: {j(a.sources)}",
             f"related: {j(a.related)}", "---", f"# {title}", "", _escape_body(a.body), "", "## Sources"]
    lines += [f"- [{label(os.path.basename(s))}]({rel_to_root}{s})" for s in a.sources]
    lines += ["", "## Related"]
    lines += [f"- [[{link_target(r)}|{label(titles.get(r, r))}]]" for r in a.related]
    return "\n".join(lines) + "\n"


def _head(text: str) -> str:
    return text[: text.find("\n---", 3)] if text.startswith("---") and text.find("\n---", 3) != -1 else ""


def split_body(text: str) -> str:
    """Article text without frontmatter and without the trailing Sources/Related sections."""
    if _head(text):
        text = text[text.find("\n---", 3) + 4:]
    cut = text.rfind(_SRC_HDR)
    return (text[:cut] if cut != -1 else text).strip()


def _json_list(head: str, key: str) -> list[str]:
    m = re.search(rf"^{key}:[ \t]*(\[.*\])[ \t]*$", head, re.M)
    try:
        v = json.loads(m.group(1)) if m else []
    except json.JSONDecodeError:
        return []
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _json_str(head: str, key: str, default: str) -> str:
    m = re.search(rf"^{key}:[ \t]*(.*)$", head, re.M)
    if not m:
        return default
    raw = m.group(1).strip()
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        return raw or default
    return v if isinstance(v, str) else default


def parse(path: str, text: str) -> Article:
    """`sources`/`related` come ONLY from the frontmatter written by `render`, never from the body."""
    head = _head(text)
    body = re.sub(r"^# .*\n?", "", split_body(text), count=1).strip()
    return Article(path, _json_str(head, "title", path), body,
                   [s for s in _json_list(head, "sources") if s.startswith("raw/")],
                   [r for r in _json_list(head, "related") if r.endswith(".md")],
                   _json_str(head, "updated", ""))
