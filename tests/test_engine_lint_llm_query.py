"""Lint grounding quality, SSRF/LLM client hardening, and query audit/citation behaviour."""
import http.server
import json
import threading
import urllib.request

import pytest

from llmwiki.engine.lint import check_text
from llmwiki.engine.llm import FakeLLM, LLMError, OpenAICompatClient
from llmwiki.engine.query import AccessDenied, NO_EVIDENCE
from test_engine_support import UA, make_env, triage


def kinds(body, raw):
    return sorted(check_text(body, raw))


# -- lint ------------------------------------------------------------------------------------
def test_numbers_use_boundaries_and_normalization():
    assert kinds("매출 20", "날짜 2026-09-30 합계 1,120") == [("number", "20")]  # not a substring of 2026 / 1,120
    assert kinds("매출 1200만원", "매출 1,200만원") == []  # comma-insensitive
    assert kinds("매출 1,200만원", "매출 1200만원") == []
    assert kinds("합계 1,200", "합계 11,200") == [("number", "1,200")]
    assert kinds("값 3.5", "값 13.5") == [("number", "3.5")]
    assert kinds("값 1,200.", "값 1,200") == []  # sentence period


def test_korean_and_slash_dates_compared_as_ymd():
    raw = "회의는 2026-09-30 에 진행"
    assert kinds("2026년 9월 30일 회의", raw) == []
    assert kinds("2026.09.30 회의", raw) == []
    assert kinds("2026/9/30 회의", raw) == []
    assert kinds("2026년 9월 31일 회의", raw) == [("date", "2026년 9월 31일")]
    assert kinds("2026.10.30", raw) == [("date", "2026.10.30")]
    assert kinds("2026/09/29", raw) == [("date", "2026/09/29")]
    assert kinds("2026-09-31", raw) == [("date", "2026-09-31")]
    assert kinds("회의 2026년 9월 30일", "2026년 09월 30일 회의") == []  # raw may use the Korean form too


def test_more_quote_styles_checked():
    raw = "그가 말했다 정확한 원문 인용 문장 입니다"
    assert kinds("‘정확한 원문 인용 문장’", raw) == []
    assert kinds("『정확한 원문 인용 문장』", raw) == []
    assert kinds("‘지어낸 아주 긴 인용 문장’", raw) == [("quote", "지어낸 아주 긴 인용 문장")]
    assert kinds("『지어낸 아주 긴 인용 문장』", raw) == [("quote", "지어낸 아주 긴 인용 문장")]
    assert kinds("‘짧은’", raw) == []  # under 10 chars is ignored


def test_source_footer_lines_not_linted():
    body = "내용 없음\n(Source: raw/dept-2026/2026-09-30-weekly-3.md)"
    assert kinds(body, "내용 없음") == []


# -- SSRF -------------------------------------------------------------------------------------
def fake_dns(monkeypatch, *ips):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", (ip, 0)) for ip in ips])


@pytest.mark.parametrize("url", ["ftp://127.0.0.1/v1", "file:///etc/passwd", "gopher://10.0.0.1", "127.0.0.1:8000",
                                 "http://0x08080808/v1", "http://134744072/v1", "http://0x7f.1/v1",
                                 "http://8.8.8.8/v1", "http://[::ffff:8.8.8.8]/v1", "http://0.0.0.0/v1", "http:///v1"])
def test_ssrf_rejected_without_network(monkeypatch, url):
    def boom(*a, **k):
        raise OSError("no network in tests")
    monkeypatch.setattr("socket.getaddrinfo", boom)
    with pytest.raises(ValueError):
        OpenAICompatClient(url, "m")


def test_hostname_resolution_must_be_all_internal(monkeypatch):
    fake_dns(monkeypatch, "10.0.0.5")
    OpenAICompatClient("http://llm-server:8000/v1", "m")  # single label ok only because it resolves privately
    OpenAICompatClient("https://llm.corp.example/v1", "m")
    fake_dns(monkeypatch, "10.0.0.5", "8.8.8.8")  # one public answer is enough to refuse
    with pytest.raises(ValueError):
        OpenAICompatClient("http://llm-server:8000/v1", "m")
    fake_dns(monkeypatch, "93.184.216.34")
    with pytest.raises(ValueError):
        OpenAICompatClient("http://intranet.local/v1", "m")  # suffix alone is not trust
    with pytest.raises(ValueError):
        OpenAICompatClient("http://nodot/v1", "m")  # single label resolving publicly


def test_complete_rechecks_dns(monkeypatch):
    fake_dns(monkeypatch, "10.0.0.5")
    c = OpenAICompatClient("http://llm-server/v1", "m")
    fake_dns(monkeypatch, "8.8.8.8")  # rebinding after construction
    with pytest.raises(LLMError):
        c.complete("s", "p")


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        if self.path.endswith("/redir/chat/completions"):
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.end_headers()
            return
        payload = {"/ok": {"choices": [{"message": {"content": "hi"}}]}, "/nochoices": {"error": "x"},
                   "/empty": {"choices": []}, "/null": {"choices": [{"message": {"content": None}}]}}
        key = next((k for k in list(payload) + ["/500", "/html"] if self.path.startswith(k)), "/html")
        if key == "/500":
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html>" if key == "/html" else json.dumps(payload[key]).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    s = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


def test_proxy_environment_is_ignored(server, monkeypatch):
    for k in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")  # dead proxy: a proxied request would fail
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert OpenAICompatClient(server + "/ok", "m").complete("s", "p") == "hi"


def test_client_errors_are_llmerror_and_redirects_refused(server):
    assert OpenAICompatClient(server + "/ok", "m").complete("s", "p") == "hi"
    for path in ("/nochoices", "/empty", "/null", "/500", "/html", "/redir"):
        with pytest.raises(LLMError):
            OpenAICompatClient(server + path, "m", timeout=5).complete("s", "p")
    with pytest.raises(LLMError):
        OpenAICompatClient("http://127.0.0.1:9/v1", "m", timeout=2).complete("s", "p")  # connection refused


# -- query ------------------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path / "data")
    e.ingest("dept-a", "a.md", "알파 프로젝트 예산", triage(title="알파 프로젝트", topic="p", body="알파 프로젝트 예산 100"))
    e.ingest("dept-a", "b.md", "알파 계획 예산", triage(title="알파 계획", topic="p", body="알파 계획 예산 200"))
    return e


def test_failed_llm_call_still_leaves_audit_trace(env):
    def boom(system, prompt):
        raise RuntimeError("down")
    with pytest.raises(RuntimeError):
        env.query_service(FakeLLM(boom)).query(UA, "알파 예산")
    rows = [(a, d) for _, u, a, _, d in env.audit.entries() if u == "ua"]
    assert rows[0][0] == "query_intent" and rows[0][1].startswith("context: ")
    assert rows[-1] == ("query", "llm-error: RuntimeError")


def test_read_article_nul_and_weird_paths_audited_as_denial(env):
    qs = env.query_service()
    for bad in ("a\x00b.md", "p/알파-프로젝트.md\x00", "x" * 5000 + ".md", "C:/Windows/win.ini"):
        with pytest.raises(AccessDenied):
            qs.read_article(UA, bad)
    assert sum(1 for _, _, a, _, _ in env.audit.entries() if a == "read_denied") == 4


def test_citations_only_for_markers_used(env):
    # context numbers articles [1],[2]; the answer cites only [2] and an invalid [7]
    llm = FakeLLM(lambda s, p: "예산은 200 입니다 [2] 그리고 [7].")
    qs = env.query_service(llm)
    res = qs.query(UA, "알파 예산")
    assert len(res.citations) == 1 and "[7]" not in res.answer and "[2]" in res.answer
    hits = [l.split(" ", 1)[1] for l in llm.calls[0][1].splitlines() if l.startswith("[2] ")]
    assert res.citations == hits
    assert any(d.startswith("cited: ") and "invalid" in d for *_, d in env.audit.entries())
    # only invalid markers -> answer keeps no bogus marker and cites nothing
    res = env.query_service(FakeLLM(lambda s, p: "근거 [9]")).query(UA, "알파 예산")
    assert res.citations == [] and "[9]" not in res.answer
    # no markers at all -> all retrieved articles (previous behaviour)
    res = env.query_service(FakeLLM(lambda s, p: "마커 없음")).query(UA, "알파 예산")
    assert len(res.citations) == 2
    assert env.query_service(FakeLLM(lambda s, p: NO_EVIDENCE)).query(UA, "알파 예산").citations == []


def test_audit_does_not_store_answers(env):
    env.query_service(FakeLLM(lambda s, p: "비밀답변-XYZ [1]")).query(UA, "알파 예산")
    assert all("비밀답변-XYZ" not in "".join(map(str, row)) for row in env.audit.entries())
