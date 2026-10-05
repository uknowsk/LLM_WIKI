"""Context budget packing, ContextExceeded retry and HTTP 400 parsing (no real network)."""
from __future__ import annotations

import http.server
import json
import threading

import pytest

from llmwiki.engine.llm import ContextExceeded, FakeLLM, LLMError, OpenAICompatClient
from llmwiki.engine.params import RetrievalParams, context_char_budget
from llmwiki.engine.query import NO_EVIDENCE, TRUNCATED, QueryService, pack_context
from test_engine_support import UA, make_env, triage

Q = "휴가 신청 절차"
LONG = "휴가 신청 절차 안내 " * 200  # ~1400 chars


def test_budget_env_parsing(monkeypatch):
    monkeypatch.delenv("WIKI_LLM_CONTEXT_TOKENS", raising=False)
    assert context_char_budget() == int((8192 - 1500) * 0.9)
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", "32768")
    assert context_char_budget() == int((32768 - 1500) * 0.9)
    for bad in ("abc", "", "-5", "0", "1.5"):
        monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", bad)
        assert context_char_budget() == int((8192 - 1500) * 0.9)
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", "100")
    assert context_char_budget() == 1500  # floor
    assert context_char_budget(4096) == int((4096 - 1500) * 0.9)


def test_params_from_env(monkeypatch):
    monkeypatch.delenv("WIKI_TOP_K", raising=False)
    monkeypatch.delenv("WIKI_MAX_CONTEXT_CHARS", raising=False)
    assert RetrievalParams.from_env() == RetrievalParams()  # the tuned defaults, not the legacy k=5
    monkeypatch.setenv("WIKI_TOP_K", "3")
    monkeypatch.setenv("WIKI_MAX_CONTEXT_CHARS", "2000")
    p = RetrievalParams.from_env()
    assert (p.top_k, p.max_context_chars) == (3, 2000)
    for bad in ("abc", "", "0", "-2", "1.5"):  # invalid -> default (top_k must be >= 1)
        monkeypatch.setenv("WIKI_TOP_K", bad)
        monkeypatch.setenv("WIKI_MAX_CONTEXT_CHARS", bad)
        p = RetrievalParams.from_env()
        assert p.top_k == RetrievalParams().top_k
        assert p.max_context_chars == (0 if bad == "0" else RetrievalParams().max_context_chars)


def test_web_query_uses_env_params(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from llmwiki.web.api import query_service
    monkeypatch.setenv("WIKI_TOP_K", "1")
    env = make_env(tmp_path)
    app = SimpleNamespace(settings=env.settings, store=env.store, llm=FakeLLM(), audit=env.audit, embedder=None)
    assert query_service(app).params.top_k == 1


def test_pack_order_truncation_and_drop():
    docs = {"a.md": "A" * 1000, "b.md": "B" * 1000, "c.md": "C" * 1000}
    inc, ctx, tr = pack_context(docs, 10_000, None)
    assert inc == ["a.md", "b.md", "c.md"] and not tr and TRUNCATED not in ctx
    assert ctx.index("[1] a.md") < ctx.index("[2] b.md") < ctx.index("[3] c.md")
    inc, ctx, tr = pack_context(docs, 1700, None)  # b fits partially (>300 left)
    assert inc == ["a.md", "b.md"] and tr and ctx.endswith(TRUNCATED) and "C" not in ctx
    assert len(ctx) <= 1700
    inc, ctx, tr = pack_context(docs, 1100, None)  # <300 left: c/b dropped, not truncated
    assert inc == ["a.md"] and tr and "B" not in ctx


def test_first_article_always_included_and_cut():
    inc, ctx, tr = pack_context({"a.md": "A" * 5000, "b.md": "B"}, 500, None)
    assert inc == ["a.md"] and tr and TRUNCATED in ctx and len(ctx) <= 500
    inc, ctx, _ = pack_context({"a.md": "A" * 5000}, 10, None)  # tiny budget: still the first hit
    assert inc == ["a.md"] and ctx.startswith("[1] a.md")


def _env(tmp_path, n=3, body=LONG):
    env = make_env(tmp_path)
    for i in range(n):
        env.ingest("dept-a", f"a{i}.md", body, triage(title=f"휴가 {i}", body=body))
    return env


def test_markers_and_citations_only_for_included(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", "1900")  # budget 1500 chars: ~1 article fits fully
    env = _env(tmp_path)
    llm = FakeLLM(lambda s, p: "답변 [1] [2] [3]")
    res = QueryService(env.settings, env.store, llm, env.audit).query(UA, Q, params=RetrievalParams(top_k=3))
    prompt = llm.calls[0][1]
    n = prompt.count("\n[") + (1 if prompt.startswith("[") else 0)
    included = [m for m in ("[1] ", "[2] ", "[3] ") if m in prompt]
    assert len(res.citations) == len(included) < 3
    assert "[3]" not in res.answer and len(prompt) < 2500
    assert n >= 1


def test_audit_has_counts_not_text(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", "1900")
    env = _env(tmp_path)
    QueryService(env.settings, env.store, FakeLLM(lambda s, p: "ok [1]"), env.audit).query(UA, Q, params=RetrievalParams(top_k=3))
    text = repr(env.audit.entries())
    assert "hits=3" in text and "truncated=True" in text and "휴가 신청 절차 안내" not in text


class Scripted:
    def __init__(self, fails, n_ctx=None):
        self.fails, self.n_ctx, self.prompts = fails, n_ctx, []

    def complete(self, system, prompt, temperature=None):
        self.prompts.append(prompt)
        if len(self.prompts) <= self.fails:
            raise ContextExceeded("x", self.n_ctx)
        return "ok [1]"


def test_context_exceeded_retries_with_halved_budget(tmp_path):
    env = _env(tmp_path, body="휴가 " * 1500)
    llm = Scripted(1)
    res = QueryService(env.settings, env.store, llm, env.audit).query(UA, Q, params=RetrievalParams(top_k=3))
    assert res.answer == "ok [1]" and len(llm.prompts) == 2
    assert len(llm.prompts[1]) < len(llm.prompts[0]) * 0.6


def test_retry_uses_known_n_ctx(tmp_path):
    env = _env(tmp_path, body="휴가 " * 3000)
    llm = Scripted(1, n_ctx=2048)
    QueryService(env.settings, env.store, llm, env.audit).query(UA, Q, params=RetrievalParams(top_k=3))
    assert len(llm.prompts[1]) <= int(0.8 * context_char_budget(2048)) + 200


def test_second_failure_propagates(tmp_path):
    env = _env(tmp_path)
    llm = Scripted(2)
    with pytest.raises(LLMError):
        QueryService(env.settings, env.store, llm, env.audit).query(UA, Q, params=RetrievalParams(top_k=3))
    assert len(llm.prompts) == 2


def test_other_llm_errors_not_retried(tmp_path):
    env = _env(tmp_path)
    calls = []

    def boom(s, p):
        calls.append(1)
        raise LLMError("down")
    with pytest.raises(LLMError):
        QueryService(env.settings, env.store, FakeLLM(boom), env.audit).query(UA, Q)
    assert len(calls) == 1


def test_unreadable_never_in_prompt(tmp_path):
    env = _env(tmp_path, n=1)
    env.ingest("dept-b", "s.md", "휴가 신청 절차 SECRETMARK", triage(title="비밀", body="휴가 신청 절차 SECRETMARK"))
    llm = FakeLLM(lambda s, p: NO_EVIDENCE)
    QueryService(env.settings, env.store, llm, env.audit).query(UA, Q)
    assert "SECRETMARK" not in llm.calls[0][1]


BODIES = {
    "/ctx": (400, {"error": {"code": 400, "message": "request (20030 tokens) exceeds the available context size (8192 tokens)",
                             "type": "exceed_context_size_error", "n_prompt_tokens": 20030, "n_ctx": 8192}}),
    "/ctx413": (413, {"error": {"message": "request exceeds the available context size"}}),
    "/other": (400, {"error": {"message": "bad request"}}),
}


class _H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        code, body = BODIES[self.path.split("/chat")[0]]
        self.send_response(code)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    s = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


@pytest.mark.parametrize("rf", [None, {"type": "json_object"}])
def test_http_400_body_parsed(server, rf):
    with pytest.raises(ContextExceeded) as ei:
        OpenAICompatClient(server + "/ctx", "m").complete("s", "SECRET PROMPT", response_format=rf)
    assert ei.value.n_ctx == 8192 and "SECRET PROMPT" not in str(ei.value)
    assert isinstance(ei.value, LLMError)


def test_http_413_and_other_400(server):
    with pytest.raises(ContextExceeded) as ei:
        OpenAICompatClient(server + "/ctx413", "m").complete("s", "p")
    assert ei.value.n_ctx is None
    with pytest.raises(LLMError) as e2:
        OpenAICompatClient(server + "/other", "m").complete("s", "p")
    assert not isinstance(e2.value, ContextExceeded)
