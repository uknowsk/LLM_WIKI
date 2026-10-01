"""Shared helpers for eval-harness tests: a tiny synthetic corpus/QA, built through the real pipeline with fakes."""
from __future__ import annotations

import json
from pathlib import Path

from llmwiki.engine.embed import FakeEmbedder
from llmwiki.engine.llm import FakeLLM
from llmwiki.eval.build import build
from llmwiki.eval.runner import Harness, settings_for
from llmwiki.eval.dataset import load_qa
from test_pipeline_support import responder

DOCS = {
    "dept-a/alpha.md": "# Alpha printer budget\n\nThe alpha printer budget is 1,200만원 approved on 2026년 9월 30일.\n",
    "dept-a/beta.md": "# Beta backup window\n\nThe beta backup job takes 45분 every night on server nas9.\n",
    "dept-a/delta.md": "# Delta vpn notice\n\nThe delta vpn gateway moves to the new datacenter in March.\n",
    "dept-b/gamma.md": "# Gamma secret budget\n\nThe gamma secret budget profit is 21억 3,000만원 this quarter.\n",
}


def qa_rows() -> list[dict]:
    def row(id, space, q, typ, docs, facts, split, **kw):
        return {"id": id, "space": space, "question": q, "type": typ, "gold_docs": [f"eval/corpus/{d}" for d in docs],
                "gold_facts": facts, "gold_answer": "", "split": split, **kw}
    return [
        row("q1", "dept-a", "alpha printer budget amount", "lookup", ["dept-a/alpha.md"], ["1200만원"], "tune"),
        row("q2", "dept-a", "beta backup job duration", "numeric", ["dept-a/beta.md"], ["45 분"], "tune"),
        row("q3", "dept-a", "zzzz qqqq xxxx", "unanswerable", [], [], "tune"),
        row("q4", "dept-a", "gamma secret budget profit", "cross_space", [], [], "tune", leak_facts=["21억 3,000만원"]),
        row("q5", "dept-a", "delta vpn gateway datacenter", "paraphrase", ["dept-a/delta.md"], ["March"], "heldout"),
        row("q6", "dept-b", "gamma secret budget profit", "lookup", ["dept-b/gamma.md"], ["21억3000만원"], "heldout"),
    ]


def write_corpus(root: Path) -> Path:
    corpus = root / "eval" / "corpus"
    for rel, text in DOCS.items():
        p = corpus / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return corpus


def write_qa(root: Path, rows: list[dict] | None = None) -> Path:
    p = root / "qa.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in (rows or qa_rows())) + "\n", encoding="utf-8")
    return p


def make_snapshot(root: Path):
    """(snapshot dir, qa path) built with FakeLLM + FakeEmbedder through process_file."""
    corpus, qa = write_corpus(root), write_qa(root)
    snap = root / "snap"
    build(corpus, settings_for(snap), FakeLLM(responder), FakeEmbedder())
    return snap, qa


def make_harness(root: Path, llm=None, with_embedder: bool = True) -> Harness:
    snap, qa = make_snapshot(root)
    return Harness(settings_for(snap), load_qa(qa, "all"), llm, FakeEmbedder() if with_embedder else None)


def echo_llm() -> FakeLLM:
    return FakeLLM(lambda system, prompt: prompt)  # echoes the context (with [n] markers)
