"""Conflict visibility: an article with a '## Disputed' section is flagged on the citation the user sees, and the
flag never reveals anything about articles the user may not read. lint_disputed lists them for admins."""
import pytest

from llmwiki.engine.lint import lint_disputed
from llmwiki.engine.llm import FakeLLM
from test_engine_support import triage
from test_web_support import make_web

DISPUTED_A = "알파 예산 협의 1,200만원\n\n## Disputed\n다른 문서는 1,500만원이라고 함\n(Source: raw/dept-a/a.md)"
DISPUTED_B = "알파 기밀 예산 협의\n\n## Disputed\n기밀 충돌 내용\n(Source: raw/dept-b/b.md)"


@pytest.fixture
def w(tmp_path):
    w = make_web(tmp_path, llm=FakeLLM())
    e = w.env
    e.ingest("dept-a", "a.md", "알파 예산 협의", triage(title="알파예산", topic="project", body=DISPUTED_A))
    e.ingest("dept-a", "c.md", "알파 일정 협의", triage(title="알파일정", topic="project", body="알파 일정 협의는 문제 없음"))
    e.ingest("dept-b", "b.md", "알파 기밀 예산 협의", triage(title="베타기밀", topic="project", body=DISPUTED_B))
    return w


def test_citation_is_flagged_when_the_article_has_a_disputed_section(w):
    c = w.client()
    c.login("ua")
    r = c.post("/api/query", {"question": "알파 예산 협의"})
    assert r.status == 200
    flags = {x["title"]: x["disputed"] for x in r.json()["citations"]}
    assert flags == {"알파예산": True, "알파일정": False}


def test_flag_and_response_never_mention_unreadable_articles(w):
    c = w.client()
    c.login("ua")
    body = c.post("/api/query", {"question": "알파 기밀 예산 협의"}).body.decode()
    assert "베타기밀" not in body and "dept-b" not in body and "기밀 충돌" not in body


def test_lint_disputed_lists_every_disputed_article_for_admins(w):
    found = lint_disputed(w.env.settings, w.env.store)
    titles = {w.env.store.article_title(p) for p in found}
    assert titles == {"알파예산", "베타기밀"} and found == sorted(found)


def test_admin_cli_lists_disputed_articles(w, capsys, monkeypatch):
    from llmwiki.lint_admin import main
    monkeypatch.setenv("WIKI_DATA_DIR", str(w.env.settings.data_dir))
    monkeypatch.setenv("WIKI_ENV", "development")
    assert main(["disputed"]) == 0
    out = capsys.readouterr().out
    assert "알파예산" in out and "베타기밀" in out and "알파일정" not in out
    assert all("\t" in line for line in out.strip().splitlines())


def test_heading_must_be_a_real_section_not_a_mention(tmp_path):
    w = make_web(tmp_path, llm=FakeLLM())
    w.env.ingest("dept-a", "d.md", "설명 문서", triage(title="설명", topic="project",
                 body="이 문서는 '## Disputed' 라는 제목 형식을 설명한다. 본문 중간의 ## Disputed 는 절이 아니다."))
    assert lint_disputed(w.env.settings, w.env.store) == []
