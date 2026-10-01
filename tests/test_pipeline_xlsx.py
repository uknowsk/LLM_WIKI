"""End to end: merged-cell .xlsx / .docx dropped in the inbox become articles with expanded cells."""
from __future__ import annotations

from test_ingest_office import _docx, _tc, _tr, _xlsx
from test_pipeline_support import make_env

from llmwiki.web.upload import ALLOWED_EXT


def _raw_text(env, space: str) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted((env.settings.raw_dir / space).glob("*.md")))


def test_xlsx_is_accepted_by_pipeline_and_upload():
    assert ".xlsx" in ALLOWED_EXT


def test_merged_cell_xlsx_flows_to_an_article(tmp_path):
    env = make_env(tmp_path)
    env.drop("dept-a", "budget.xlsx", _xlsx())
    res = env.run_all()
    assert [r.status for r in res] == ["done"], [r.error for r in res]
    raw = _raw_text(env, "dept-a")
    # the merged '상반기' header is repeated over every spanned column, the merged label on every row
    assert raw.count("상반기 > ") >= 6
    assert "재무 | 비용" in raw or "재무 / 비용" in raw
    assert len(env.store.article_paths()) == 1
    assert "budget.xlsx" not in {p.name for p in (env.settings.data_dir / "inbox" / "dept-a").glob("*")}


def test_merged_cell_docx_flows_to_an_article(tmp_path):
    env = make_env(tmp_path)
    body = _tr(_tc("구분", span=1), _tc("상반기", span=2)) + _tr(_tc("재무", vm="restart"), _tc("100"), _tc("200"))
    env.drop("dept-a", "form.docx", _docx("<w:tbl>" + body + "</w:tbl>"))
    res = env.run_all()
    assert [r.status for r in res] == ["done"], [r.error for r in res]
    assert "재무" in _raw_text(env, "dept-a")
