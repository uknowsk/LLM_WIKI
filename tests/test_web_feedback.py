"""Answer feedback (POST /api/feedback {rating: up|down, paths: [...]}): one audit row per cited article, never the
question or answer text, only for articles the user may read, and an admin report ranks the articles marked wrong."""
import pytest

from llmwiki.engine.llm import FakeLLM
from test_engine_support import triage
from test_web_support import make_web

QUESTION = "알파 예산 협의는 얼마?"


@pytest.fixture
def w(tmp_path):
    w = make_web(tmp_path, llm=FakeLLM())
    e = w.env
    e.ingest("dept-a", "a.md", "알파 예산", triage(title="알파예산", topic="project", body="알파 예산 협의 1,200만원"))
    e.ingest("dept-b", "b.md", "베타 기밀", triage(title="베타기밀", topic="project", body="베타 기밀 문서"))
    return w


def path_of(w, title):
    return next(p for p in w.env.store.article_paths() if w.env.store.article_title(p) == title)


def fb_rows(w):
    return [r for r in w.env.audit.entries() if r[2].startswith("feedback")]


def login(w, uid="ua"):
    c = w.client()
    assert c.login(uid).status == 200
    return c


def test_feedback_is_recorded_per_path_without_the_question(w):
    c = login(w)
    pa = path_of(w, "알파예산")
    c.post("/api/query", {"question": QUESTION})
    r = c.post("/api/feedback", {"rating": "down", "paths": [pa]})
    assert r.status == 200 and r.json() == {"ok": True}
    rows = fb_rows(w)
    assert [(a, t) for _, _, a, t, _ in rows] == [("feedback_down", pa)]
    assert QUESTION not in " ".join(str(x) for x in w.env.audit.entries() if x[2].startswith("feedback"))


def test_up_without_citations_is_recorded_once(w):
    c = login(w)
    assert c.post("/api/feedback", {"rating": "up", "paths": []}).status == 200
    assert [(a, t) for _, _, a, t, _ in fb_rows(w)] == [("feedback_up", "-")]


def test_unreadable_or_missing_article_is_404_and_nothing_is_recorded(w):
    c = login(w)
    pa, pb = path_of(w, "알파예산"), path_of(w, "베타기밀")
    answers = [c.post("/api/feedback", {"rating": "down", "paths": p}) for p in ([pb], ["no/such.md"], ["../x"], [pa, pb])]
    assert all(r.status == 404 and r.body == answers[0].body == b'{"error": "not_found"}' for r in answers)
    assert not any(a in ("feedback_up", "feedback_down") for _, _, a, _, _ in fb_rows(w))  # all-or-nothing


@pytest.mark.parametrize("payload", [
    {"rating": "meh", "paths": []}, {"paths": []}, {"rating": "up"}, {"rating": "up", "paths": "x"},
    {"rating": "up", "paths": [5]}, {"rating": "up", "paths": ["a"] * 9}, {"rating": 1, "paths": []},
])
def test_invalid_payload_is_400(w, payload):
    c = login(w)
    r = c.post("/api/feedback", payload)
    assert r.status == 400 and r.json() == {"error": "bad_feedback"}
    assert fb_rows(w) == []


def test_requires_session_and_csrf(w):
    assert w.client().post("/api/feedback", {"rating": "up", "paths": []}, csrf=False).status in (401, 403)
    c = login(w)
    assert c.post("/api/feedback", {"rating": "up", "paths": []}, csrf=False).status == 403
    assert fb_rows(w) == []


def test_admin_report_ranks_articles_marked_wrong(w, capsys, monkeypatch):
    from llmwiki.lint_admin import main
    c = login(w)
    pa = path_of(w, "알파예산")
    for rating in ("down", "down", "up"):
        assert c.post("/api/feedback", {"rating": rating, "paths": [pa]}).status == 200
    monkeypatch.setenv("WIKI_DATA_DIR", str(w.env.settings.data_dir))
    monkeypatch.setenv("WIKI_ENV", "development")
    assert main(["feedback"]) == 0
    assert capsys.readouterr().out.strip().splitlines() == [f"2\t1\t{pa}\t알파예산"]
