"""Admin reports built only from the existing audit log: `gaps` (questions that ended in "no evidence", most frequent
first, i.e. what the wiki does not know yet) and `stats` (usage counters)."""
import pytest

from llmwiki.engine.llm import FakeLLM
from llmwiki.lint_admin import main
from test_engine_support import triage
from test_web_support import make_web


@pytest.fixture
def w(tmp_path, monkeypatch):
    w = make_web(tmp_path, llm=FakeLLM(lambda s, p: "답변 [1]"))
    w.env.ingest("dept-a", "a.md", "서버 증설 일정 11월", triage(title="서버증설", topic="project", body="서버 증설 일정은 11월이다"))
    monkeypatch.setenv("WIKI_DATA_DIR", str(w.env.settings.data_dir))
    monkeypatch.setenv("WIKI_ENV", "development")
    c = w.client()
    assert c.login("ua").status == 200
    for q in ("복지 포인트 정책은?", "복지 포인트 정책은?", "  복지 포인트 정책은?  ", "연차 이월 규정?", "서버 증설 일정은?"):
        assert c.post("/api/query", {"question": q}).status == 200
    c.post("/api/feedback", {"rating": "down", "paths": []})
    w.c = c
    return w


def test_gaps_lists_unanswered_questions_most_frequent_first(w, capsys):
    assert main(["gaps"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [ln.split("\t")[:2] for ln in lines] == [["3", "복지 포인트 정책은?"], ["1", "연차 이월 규정?"]]
    assert "서버 증설 일정은?" not in "\n".join(lines)  # answered questions are not gaps


def test_gaps_limit(w, capsys):
    assert main(["gaps", "--limit", "1"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_stats_counters(w, capsys):
    assert main(["stats"]) == 0
    stats = dict(ln.split("\t") for ln in capsys.readouterr().out.strip().splitlines())
    assert stats["queries"] == "5" and stats["no_evidence"] == "4" and stats["users"] == "1"
    assert stats["feedback_down"] == "1" and stats["feedback_up"] == "0"
