"""Article markdown format (Obsidian-compatible). Sources/Related sections always come last."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_SRC_HDR = "\n## Sources"
_REL_HDR = "\n## Related"
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]")


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


def render(a: Article, titles: dict[str, str]) -> str:
    rel_to_root = "../" * (a.path.count("/") + 1)  # article lives under <data>/wiki/
    lines = ["---", f"title: {a.title}", f"updated: {a.updated}", "sources:"]
    lines += [f"  - {s}" for s in a.sources]
    lines += ["---", f"# {a.title}", "", a.body.strip(), "", "## Sources"]
    lines += [f"- [{os.path.basename(s)}]({rel_to_root}{s})" for s in a.sources]
    lines += ["", "## Related"]
    lines += [f"- [[{link_target(r)}|{titles.get(r, r)}]]" for r in a.related]
    return "\n".join(lines) + "\n"


def _head(text: str) -> str:
    return text[: text.find("\n---", 3)] if text.startswith("---") and text.find("\n---", 3) != -1 else ""


def split_body(text: str) -> str:
    """Article text without frontmatter and without the trailing Sources/Related sections."""
    if _head(text):
        text = text[text.find("\n---", 3) + 4:]
    cut = text.find(_SRC_HDR)
    return (text[:cut] if cut != -1 else text).strip()


def parse(path: str, text: str) -> Article:
    head = _head(text)
    title = re.search(r"^title:\s*(.*)$", head, re.M)
    updated = re.search(r"^updated:\s*(.*)$", head, re.M)
    sources = re.findall(r"^\s+-\s+(\S.*)$", head, re.M)
    body = re.sub(r"^# .*\n?", "", split_body(text), count=1).strip()
    rel_at = text.find(_REL_HDR)
    related = [m + ".md" for m in _WIKILINK.findall(text[rel_at:])] if rel_at != -1 else []
    return Article(path, title.group(1).strip() if title else path, body, sources, related,
                   updated.group(1).strip() if updated else "")
