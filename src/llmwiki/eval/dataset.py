"""QA dataset + gold map loading. Doc ids are corpus-relative posix paths ('dept-a/x.md')."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ANSWERABLE = ("lookup", "paraphrase", "numeric", "multi_doc", "latest", "table_merged")
REFUSAL_TYPES = ("unanswerable", "cross_space")
TYPES = ANSWERABLE + REFUSAL_TYPES
SPLITS = ("tune", "heldout")


def doc_id(path: str) -> str:
    """'eval/corpus/dept-a/x.md' (or its Windows-separator form) -> 'dept-a/x.md'."""
    p = str(path).replace("\\", "/")
    parts = p.split("/")
    if "corpus" in parts:
        parts = parts[len(parts) - 1 - parts[::-1].index("corpus") + 1:]
    return "/".join(parts)


@dataclass(frozen=True)
class QA:
    id: str
    space: str
    question: str
    type: str
    gold_docs: tuple[str, ...] = ()
    gold_facts: tuple[str, ...] = ()
    gold_answer: str = ""
    split: str = "tune"
    leak_facts: tuple[str, ...] = ()
    stale_docs: tuple[str, ...] = field(default=())

    @property
    def answerable(self) -> bool:
        return self.type in ANSWERABLE


def load_qa(path: Path | str, split: str = "all") -> list[QA]:
    if split not in SPLITS + ("all",):
        raise ValueError(f"split must be tune|heldout|all, got {split!r}")
    out: list[QA] = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            d = json.loads(line)
            if d["type"] not in TYPES:
                raise ValueError(f"line {n}: unknown type {d['type']!r}")
            if d.get("split", "tune") not in SPLITS:
                raise ValueError(f"line {n}: unknown split {d.get('split')!r}")
            q = QA(d["id"], d["space"], d["question"], d["type"],
                   tuple(doc_id(x) for x in d.get("gold_docs", [])), tuple(d.get("gold_facts", [])),
                   d.get("gold_answer", ""), d.get("split", "tune"), tuple(d.get("leak_facts", [])),
                   tuple(doc_id(x) for x in d.get("stale_docs", [])))
            if split == "all" or q.split == split:
                out.append(q)
    return out


@dataclass
class GoldMap:
    """corpus doc id -> {"space", "raw_paths", "articles"} (written by `build`)."""
    docs: dict[str, dict]

    @classmethod
    def load(cls, data_dir: Path | str) -> "GoldMap":
        p = Path(data_dir) / "gold_map.json"
        if not p.is_file():
            raise FileNotFoundError(f"{p} not found: run `python -m llmwiki.eval build` first")
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def raw_to_docs(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for d, v in self.docs.items():
            for r in v.get("raw_paths", []):
                out.setdefault(r, set()).add(d)
        return out

    def relevant_articles(self, gold: set[str]) -> set[str]:
        return {a for d in gold for a in self.docs.get(d, {}).get("articles", [])}
