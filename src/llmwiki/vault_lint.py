"""Checker for the personal second-brain vault (10_raw_data / 20_ai_wiki / 30_outputs, Obsidian [[links]]).

  python -m llmwiki.vault_lint [--vault .] [--accept]

Errors (exit code 1): broken [[links]], ambiguous file names, index/file mismatch, wiki numbers/dates/quotes that are not in
the raw files it cites (the Grounding Invariant, via engine.lint.check_text), output numbers that are not in the wiki pages it
is based on, raw files changed or deleted since the manifest was accepted, malformed Status blocks, missing front matter.
Warnings: raw files not yet in the wiki, orphan wiki pages. Info: new raw files, pending list.

`--accept` records the current raw files (sha256) in 10_raw_data/.manifest.json: do it after you add raws. Raw files are the
only thing that must never change, so a later difference is reported as an error. Read-only otherwise.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .engine.lint import check_text

RAW, WIKI, OUT = "10_raw_data", "20_ai_wiki", "30_outputs"
MANIFEST = ".manifest.json"
SPECIAL = ("_index", "_log")
_LINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_STATUS = re.compile(r"(?m)^> \*\*Status: (Outdated|Disputed)\*\*(.*)$")
_INDEX_DATE = re.compile(r"\[\[([^\]\n|#]+)[^\]\n]*\]\][^\n]*?\(updated: (\d{4}-\d{2}-\d{2})\)")
_REQUIRED = {WIKI: ("topic", "updated", "sources", "status"), OUT: ("date", "based_on"), RAW: ("date", "source")}


@dataclass(frozen=True)
class Finding:
    level: str  # E | W | I
    kind: str
    file: str
    detail: str = ""


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _target(raw: str) -> str:
    return _nfc(raw.split("|", 1)[0].split("#", 1)[0].strip().removesuffix(".md"))


_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def _prose(text: str) -> str:
    """Text without code blocks/spans: `[[x]]` inside code is an example (Obsidian does not link it either)."""
    return _CODE.sub(" ", text)


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    d: dict = {}
    key = None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        s = line.strip()
        if line[0] in " \t" and s.startswith("- ") and key and isinstance(d.get(key), list):
            d[key].append(s[2:].strip().strip('"'))
        elif line[0] not in " \t" and ":" in line:
            key, _, v = line.partition(":")
            key = key.strip()
            d[key] = v.strip() or []
    return d, text[end + 4:]


def _links(items) -> list[str]:
    """[[targets]] inside a front-matter list (sources / based_on); a scalar value has none."""
    return [_target(m.group(1)) for s in (items if isinstance(items, list) else []) for m in _LINK.finditer(s)]


def _docs(vault: Path) -> dict[str, list[Path]]:
    """folder -> markdown files (templates and dot-files are not part of the vault's knowledge)."""
    out: dict[str, list[Path]] = {}
    for folder in (RAW, WIKI, OUT):
        root = vault / folder
        out[folder] = sorted(p for p in root.rglob("*.md") if not any(part == "_templates" or part.startswith(".")
                                                                    for part in p.relative_to(root).parts)) if root.is_dir() else []
    return out


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _raw_files(vault: Path) -> list[Path]:
    """Every file under 10_raw_data that the manifest protects: any type (pdf, images...), not templates or dot-files."""
    root = vault / RAW
    return sorted(p for p in root.rglob("*") if p.is_file() and not any(
        part == "_templates" or part.startswith(".") for part in p.relative_to(root).parts)) if root.is_dir() else []


STALE_DRAFT_DAYS = 30


def run_checks(vault: Path, accept: bool = False, today: date | None = None) -> list[Finding]:
    vault = Path(vault)
    today = today or date.today()
    docs = _docs(vault)

    def rel(p: Path) -> str:
        return p.relative_to(vault).as_posix()

    by_name: dict[str, list[Path]] = {}
    for files in docs.values():
        for p in files:
            by_name.setdefault(_nfc(p.stem), []).append(p)
    f: list[Finding] = []
    for name, paths in sorted(by_name.items()):
        if len(paths) > 1:
            f.append(Finding("E", "ambiguous-name", rel(paths[0]), f"{name}: " + ", ".join(rel(p) for p in paths)))

    def resolve(name: str, folder: str) -> Path | None:
        return next((p for p in by_name.get(name, []) if p.is_relative_to(vault / folder)), None)

    texts = {p: _read(p) for files in docs.values() for p in files}
    wiki_pages = [p for p in docs[WIKI] if p.stem not in SPECIAL]
    fms = {p: _frontmatter(texts[p]) for p in texts}
    hubs = {p for p in wiki_pages if fms[p][0].get("type") == "hub"}  # maps of content: no raw sources, nothing has to link to them

    # broken links (raw files are immutable and may contain anything; only wiki and outputs are checked)
    for p in docs[WIKI] + docs[OUT]:
        for m in _LINK.finditer(_prose(texts[p])):
            t = _target(m.group(1))
            if t not in by_name:
                f.append(Finding("E", "broken-link", rel(p), t))

    # front matter
    for folder in (WIKI, OUT, RAW):
        for p in docs[folder]:
            if folder == WIKI and p.stem in SPECIAL:
                continue
            need = tuple(k for k in _REQUIRED[folder] if not (p in hubs and k == "sources"))
            missing = [k for k in need if not fms[p][0].get(k)]
            if missing:
                f.append(Finding("W" if folder == RAW else "E", "missing-frontmatter", rel(p), ", ".join(missing)))

    # grounding: wiki numbers/dates/quotes must be in the raws it cites; Status blocks must be well formed
    for p in wiki_pages:
        if p in hubs:
            continue
        fm, body = fms[p]
        raws = []
        for s in _links(fm.get("sources")):
            rp = resolve(s, RAW)
            if rp is None:
                f.append(Finding("E", "bad-source", rel(p), s))
            else:
                raws.append(texts[rp])
        if raws:
            for kind, value in check_text(body, "\n".join(raws)):
                f.append(Finding("E", f"ungrounded-{kind}", rel(p), value))
        for m in _STATUS.finditer(body):
            rest, after = m.group(2).strip(), body[m.end():].lstrip("\n")
            ok = bool(rest) and after.startswith(">")
            if m.group(1) == "Outdated":
                ok = ok and re.search(r"\(\d{4}-\d{2}-\d{2}[,)]", rest) is not None
            if not ok:
                f.append(Finding("E", "bad-status", rel(p), f"Status: {m.group(1)}"))
        d = fm.get("updated")
        if str(fm.get("status")).lower() == "draft" and isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            if date.fromisoformat(d) < today - timedelta(days=STALE_DRAFT_DAYS):
                f.append(Finding("W", "stale-draft", rel(p), f"draft since {d}: finish, merge or delete it"))
    # outputs: numbers must come from the wiki pages they are based on; private pages never feed a non-private output
    for p in docs[OUT]:
        fm, body = fms[p]
        base = [wp for b in _links(fm.get("based_on")) if (wp := resolve(b, WIKI))]
        if str(fm.get("audience", "internal")).lower() != "private":
            for wp in base:
                if str(fms[wp][0].get("visibility", "")).lower() == "private":
                    f.append(Finding("E", "private-in-output", rel(p), f"{wp.stem} is private (output audience is not private)"))
        if base:
            for kind, value in check_text(body, "\n".join(texts[b] for b in base)):
                f.append(Finding("E", f"ungrounded-{kind}", rel(p), value))

    # index consistency and pending list
    index = next((p for p in docs[WIKI] if p.stem == "_index"), None)
    pending_listed: set[str] = set()
    if index is None:
        f.append(Finding("E", "no-index", f"{WIKI}/_index.md"))
    else:
        head, _, tail = texts[index].partition("## 처리 대기")
        pending_listed = {_target(m.group(1)) for m in _LINK.finditer(tail)}
        listed = {_target(m.group(1)) for m in _LINK.finditer(head)}
        names = {_nfc(p.stem) for p in wiki_pages}
        for n in sorted(names - listed):
            f.append(Finding("E", "index-missing", rel(index), n))
        for n in sorted(listed - names):
            f.append(Finding("E", "index-dangling", rel(index), n))
        dates = {_nfc(p.stem): fms[p][0].get("updated") for p in wiki_pages}
        for m in _INDEX_DATE.finditer(head):
            n = _target(m.group(1))
            if n in dates and dates[n] and dates[n] != m.group(2):
                f.append(Finding("E", "index-date", rel(index), f"{n}: index {m.group(2)} / page {dates[n]}"))

    # orphans (the index and the log do not count as inbound links)
    inbound: dict[str, set[Path]] = {}
    for p in wiki_pages:
        for m in _LINK.finditer(_prose(texts[p])):
            inbound.setdefault(_target(m.group(1)), set()).add(p)
    for p in wiki_pages:
        if p not in hubs and not (inbound.get(_nfc(p.stem), set()) - {p}):
            f.append(Finding("W", "orphan", rel(p), "no other wiki page links here"))

    # raw files: processed? immutable?
    cited = {s for p in wiki_pages for s in _links(fms[p][0].get("sources"))}
    log = next((p for p in docs[WIKI] if p.stem == "_log"), None)
    no_material = {_target(m.group(1)) for line in (texts[log].splitlines() if log else []) if "no material" in line.lower()
                   for m in _LINK.finditer(line)}
    for p in docs[RAW]:
        n = _nfc(p.stem)
        if n not in cited and n not in no_material:
            listed_pending = n in pending_listed
            f.append(Finding("I" if listed_pending else "W", "raw-pending" if listed_pending else "raw-unprocessed", rel(p)))
    raw_root = vault / RAW
    now = {p.relative_to(raw_root).as_posix(): _sha(p) for p in _raw_files(vault)}
    mpath = raw_root / MANIFEST
    if accept:
        mpath.write_text(json.dumps(dict(sorted(now.items())), ensure_ascii=False, indent=0), encoding="utf-8")
    elif not mpath.is_file():
        f.append(Finding("I", "raw-no-manifest", f"{RAW}/{MANIFEST}", "run with --accept to start protecting the raw files"))
    else:
        old = json.loads(_read(mpath))
        for k in sorted(old.keys() - now.keys()):
            f.append(Finding("E", "raw-deleted", f"{RAW}/{k}"))
        for k in sorted(now):
            if k in old and old[k] != now[k]:
                f.append(Finding("E", "raw-changed", f"{RAW}/{k}"))
            elif k not in old:
                f.append(Finding("I", "raw-new", f"{RAW}/{k}", "not in the manifest yet (--accept to register)"))
    return list(dict.fromkeys(f))  # the same problem found twice (e.g. one bad number used in two sentences) is one finding


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmwiki.vault_lint", description="Second-brain vault checker")
    ap.add_argument("--vault", default=".", help="folder that contains 10_raw_data / 20_ai_wiki / 30_outputs")
    ap.add_argument("--accept", action="store_true", help="record the current raw files as the protected baseline")
    args = ap.parse_args(argv)
    vault = Path(args.vault)
    for d in (RAW, WIKI):
        if not (vault / d).is_dir():
            print(f"missing folder: {d} (is --vault the right folder?)", file=sys.stderr)
            return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    findings = run_checks(vault, accept=args.accept)
    for x in findings:
        print(f"{x.level} {x.kind}\t{x.file}\t{x.detail}".rstrip())
    n = {lv: sum(1 for x in findings if x.level == lv) for lv in "EWI"}
    print(f"-- errors {n['E']}, warnings {n['W']}, info {n['I']}" + ("  (raw baseline recorded)" if args.accept else ""))
    return 1 if n["E"] else 0


if __name__ == "__main__":
    sys.exit(main())
