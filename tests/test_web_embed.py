"""create_app accepts an optional embedder and the query endpoint uses it (ACL-first still holds)."""
from __future__ import annotations

from llmwiki.auth import DevAuthProvider
from llmwiki.engine.embed import FakeEmbedder
from llmwiki.engine.llm import FakeLLM
from llmwiki.web import create_app
from llmwiki.web.webconfig import WebConfig
from test_engine_support import UA, UB, triage
from test_web_support import SECRET, Client, make_web


def test_query_endpoint_with_embedder_hides_other_space(tmp_path):
    w = make_web(tmp_path, llm=FakeLLM())
    emb = FakeEmbedder()
    w.env.compiler.embedder = emb
    w.env.ingest("dept-b", "b.md", "비밀 예산 계획 BMARK", triage(title="예산", body="비밀 예산 계획 BMARK"))
    w.env.ingest("dept-a", "a.md", "휴가 신청 방법", triage(title="휴가", body="휴가 신청 방법"))
    cfg = WebConfig(session_secret=SECRET.encode(), cookie_secure=False, max_upload_bytes=1000)
    app = create_app(w.env.settings, w.llm, w.env.store, w.env.audit, DevAuthProvider({UA.id: UA, UB.id: UB}),
                     config=cfg, clock=w.clock, embedder=emb)
    c = Client(app)
    assert c.login("ua").status == 200
    r = c.post("/api/query", {"question": "비밀 예산 계획"})
    assert r.status == 200 and "BMARK" not in r.body.decode("utf-8")
    assert all("예산" not in x["title"] for x in r.json()["citations"])
