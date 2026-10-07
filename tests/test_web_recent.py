"""GET /api/recent: what changed lately, limited to articles the user may read (the same single ACL query as search).
Only path, title and date are returned, never article text."""
import pytest

from llmwiki.engine.llm import FakeLLM
from test_engine_support import triage
from test_web_support import make_web


@pytest.fixture
def w(tmp_path):
    w = make_web(tmp_path, llm=FakeLLM())
    e = w.env
    e.ingest("dept-a", "a.md", "알파 문서", triage(title="알파", topic="project", body="알파 본문 SECRET-A"))
    e.ingest("dept-a", "c.md", "감마 문서", triage(title="감마", topic="project", body="감마 본문"))
    e.ingest("dept-b", "b.md", "베타 기밀", triage(title="베타기밀", topic="project", body="베타 기밀 본문 SECRET-B"))
    return w


def login(w, uid="ua"):
    c = w.client()
    assert c.login(uid).status == 200
    return c


def test_lists_only_readable_articles_with_metadata_only(w):
    r = login(w).get("/api/recent")
    assert r.status == 200
    items = r.json()["items"]
    assert sorted(i["title"] for i in items) == ["감마", "알파"]
    assert all(set(i) == {"path", "title", "updated"} and len(i["updated"]) == 10 for i in items)
    raw = r.body.decode()
    assert "베타기밀" not in raw and "dept-b" not in raw and "SECRET" not in raw


def test_other_user_sees_their_own_space_instead(w):
    items = login(w, "ub").get("/api/recent").json()["items"]
    assert [i["title"] for i in items] == ["베타기밀"]


def test_limit_is_applied_and_bounded(w):
    c = login(w)
    assert len(c.get("/api/recent", query="limit=1").json()["items"]) == 1
    for bad in ("0", "-3", "abc", "", "1000"):
        r = c.get("/api/recent", query=f"limit={bad}")
        assert r.status == 200 and len(r.json()["items"]) == 2  # invalid/too large -> default, never an error or a dump


def test_requires_a_session(w):
    assert w.client().get("/api/recent").status == 401
