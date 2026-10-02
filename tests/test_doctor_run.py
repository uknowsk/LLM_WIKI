"""python -m llmwiki.doctor against loopback stubs: healthy, failing (401/429/timeout/refused/5xx), JSON, exit codes."""
from __future__ import annotations

import json
import socket
import ssl
import time

import pytest

from llmwiki.doctor import report
from llmwiki.doctor.__main__ import main, run_checks
from llmwiki.doctor.diagnose import classify
from test_providers_support import clean_provider_env, json_reply, serve, sse_reply, write_cfg  # noqa: F401

SECRET = "LEAK-gauss-KEY-424242"
SESSION = "S" * 40


def chat_ok(content="OK"):
    return {"choices": [{"message": {"content": content}}]}


def healthy(r):
    p = r["path"]
    if p.endswith("/embeddings"):
        n = len(r["body"]["input"])
        return json_reply({"data": [{"index": i, "embedding": [1.0 + i, 2.0, 3.0 - i]} for i in range(n)]})
    b = r["body"]
    if b.get("stream"):
        return sse_reply([{"choices": [{"delta": {"content": "O"}}]}, {"choices": [{"delta": {"content": "K"}}]}])
    return json_reply(chat_ok())


def env_for(tmp_path, stub, **extra):
    e = {"WIKI_ENV": "development", "WIKI_DATA_DIR": str(tmp_path / "data"), "WIKI_LLM_BASE_URL": stub.url + "/v1",
         "WIKI_LLM_TIMEOUT": "3"}
    e.update({k: str(v) for k, v in extra.items()})
    return e


def by_id(results):
    return {r.id: r for r in results}


def test_healthy_stub_has_no_fail_and_exit_zero(tmp_path, serve, capsys):
    stub = serve(healthy)
    env = env_for(tmp_path, stub, WIKI_LLM_CONTEXT_TOKENS=65536)
    res = by_id(run_checks(env))
    assert not [r for r in res.values() if r.level == "FAIL"], [r for r in res.values() if r.level == "FAIL"]
    assert res["llm.call"].level == "PASS" and res["embed.call"].level == "PASS" and "차원=3" in res["embed.call"].detail
    assert res["llm.stream"].level == "PASS" and res["ingest.docx"].level == "PASS" and res["ingest.xlsx"].level == "PASS"
    assert res["acl.e2e"].level == "PASS" and res["fs.wal"].level == "PASS"
    assert main([], env) == 0
    out = capsys.readouterr().out
    assert "[PASS] LLM 호출 성공" in out and "요약:" in out and "[FAIL]" not in out


def test_json_output_and_exit_code(tmp_path, serve, capsys):
    stub = serve(healthy)
    assert main(["--json", "--skip-embed"], env_for(tmp_path, stub)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["exit_code"] == 0 and data["summary"]["FAIL"] == 0
    assert {"id", "level", "title", "detail", "hint"} <= set(data["results"][0])
    assert any(r["id"] == "embed.call" and r["level"] == "WARN" for r in data["results"])


@pytest.mark.parametrize("status,needle", [(401, "인증"), (403, "인증"), (429, "한도"), (500, "서버 내부"), (404, "경로"),
                                           (400, "요청 형식")])
def test_http_failures_are_classified_with_hints(tmp_path, serve, capsys, status, needle):
    stub = serve(lambda r: json_reply({"error": {"message": "nope from server"}}, status))
    env = env_for(tmp_path, stub, WIKI_EMBED_MODEL="off")
    res = by_id(run_checks(env))
    r = res["llm.call"]
    assert r.level == "FAIL" and needle in r.detail and r.hint
    assert main(["--skip-embed"], env) == 1


def test_timeout_and_refused_and_dns(tmp_path, serve):
    slow = serve(lambda r: (time.sleep(2.5), json_reply(chat_ok()))[1])
    r = by_id(run_checks(env_for(tmp_path, slow, WIKI_LLM_TIMEOUT=1, WIKI_EMBED_MODEL="off")))["llm.call"]
    assert r.level == "FAIL" and "시간 초과" in r.detail
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    env = {"WIKI_ENV": "development", "WIKI_DATA_DIR": str(tmp_path / "d2"), "WIKI_LLM_BASE_URL": f"http://127.0.0.1:{port}/v1",
           "WIKI_EMBED_MODEL": "off"}
    r = by_id(run_checks(env))["llm.call"]
    assert r.level == "FAIL" and "연결 거부" in r.detail
    env = {**env, "WIKI_LLM_BASE_URL": "http://no-such-host.invalid/v1"}
    r = by_id(run_checks(env))["llm.config"]
    assert r.level == "FAIL" and "DNS" in r.detail and "WIKI_LLM_ALLOWED_HOSTS" in r.hint


def test_classify_tls_and_proxy_hints():
    code, why, hint = classify(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
    assert code == "tls_cert" and "WIKI_LLM_CA_BUNDLE" in hint
    code, _, hint = classify(ConnectionRefusedError("refused"), proxy=True)
    assert code == "refused" and "WIKI_LLM_PROXY" in hint
    assert classify(ValueError("LLM endpoint must be on the intranet, got host 'x'"))[0] == "not_internal"
    assert "WIKI_LLM_ALLOWED_HOSTS" in classify(ValueError("LLM endpoint must be on the intranet, got host 'x'"))[2]


def test_secrets_never_appear_even_when_the_server_echoes_them(tmp_path, serve, capsys):
    stub = serve(lambda r: json_reply({"error": {"message": "bad header " + r["headers"].get("authorization", "")}}, 401))
    env = env_for(tmp_path, stub, WIKI_LLM_API_KEY=SECRET, WIKI_SESSION_SECRET=SESSION, WIKI_EMBED_MODEL="off")
    for argv in ([], ["--json"]):
        assert main(argv, env) == 1
        out = capsys.readouterr().out
        assert SECRET not in out and SESSION not in out
        assert "Bearer 키=설정됨" in out or "Bearer 키 설정됨" in out or "설정됨" in out
    assert stub.seen[0]["headers"]["authorization"] == "Bearer " + SECRET  # the key WAS sent, just never shown


def test_context_probe_reports_limit_and_suggestion(tmp_path, serve):
    def respond(r):
        n = len(r["body"]["messages"][1]["content"])
        if n > 5000:
            return json_reply({"error": {"type": "exceed_context_size_error", "message": "exceeds the available context size",
                                         "n_ctx": 4096}}, 400)
        return json_reply(chat_ok())
    stub = serve(respond)
    env = env_for(tmp_path, stub, WIKI_EMBED_MODEL="off")
    res = by_id(run_checks(env, probe_context=True, probe_sizes=(1000, 2000, 4000, 8000, 16000)))
    r = res["llm.context"]
    assert r.level == "PASS" and "4096" in r.detail and "WIKI_LLM_CONTEXT_TOKENS=4096" in r.detail


def test_context_probe_unrecognised_400_after_successes_is_treated_as_the_limit(tmp_path, serve):
    def respond(r):
        n = len(r["body"]["messages"][1]["content"])
        return json_reply({"error": {"message": "too long for me"}}, 400) if n > 5000 else json_reply(chat_ok())
    stub = serve(respond)
    env = env_for(tmp_path, stub, WIKI_EMBED_MODEL="off")
    r = by_id(run_checks(env, probe_context=True, probe_sizes=(1000, 4000, 8000)))["llm.context"]
    assert r.level == "PASS" and "통과" in r.detail and "too long for me" in r.detail and "context_exceeded_patterns" in r.hint
    assert "WIKI_LLM_CONTEXT_TOKENS=" in r.detail


def test_think_model_hint_and_adequate_settings(tmp_path, serve):
    stub = serve(lambda r: json_reply({"choices": [{"message": {"content": "<think>hmm</think>OK", "reasoning_content": "hmm"}}]}))
    env = env_for(tmp_path, stub, WIKI_EMBED_MODEL="off")
    r = by_id(run_checks(env))["llm.think"]
    assert r.level == "WARN" and "WIKI_LLM_MAX_TOKENS" in r.hint and "WIKI_LLM_TIMEOUT" in r.hint
    r = by_id(run_checks({**env, "WIKI_LLM_MAX_TOKENS": "8192", "WIKI_LLM_TIMEOUT": "300"}))["llm.think"]
    assert r.level == "PASS"


def test_structured_output_rejection_is_a_warn(tmp_path, serve):
    def respond(r):
        if "response_format" in r["body"]:
            return json_reply({"error": {"message": "unsupported"}}, 400)
        return json_reply(chat_ok())
    stub = serve(respond)
    res = by_id(run_checks(env_for(tmp_path, stub, WIKI_EMBED_MODEL="off")))
    assert res["llm.structured"].level == "WARN" and "일반 모드" in res["llm.structured"].title
    res = by_id(run_checks(env_for(tmp_path, stub, WIKI_EMBED_MODEL="off", WIKI_LLM_STRUCTURED="off")))
    assert res["llm.structured"].level == "PASS" or res["llm.structured"].level == "WARN"


def test_custom_provider_in_doctor(tmp_path, serve):
    stub = serve(lambda r: json_reply(chat_ok()) if r["headers"].get("authorization") == "Bearer k1" else json_reply({"error": {"message": "bad key"}}, 401))
    cfg = write_cfg(tmp_path, stub.url)
    env = env_for(tmp_path, stub, WIKI_LLM_PROVIDER="custom", WIKI_LLM_CUSTOM_CONFIG=cfg, WIKI_LLM_API_KEY="k1", WIKI_EMBED_MODEL="off")
    res = by_id(run_checks(env))
    assert res["llm.call"].level == "PASS" and "provider=custom" in res["llm.config"].detail
    assert res["llm.structured"].level == "PASS" and "supports_json_schema" in res["llm.structured"].detail
    res = by_id(run_checks({**env, "WIKI_LLM_API_KEY": "wrong"}))
    assert res["llm.call"].level == "FAIL" and "인증" in res["llm.call"].detail and "bad key" in res["llm.call"].detail


def test_bad_provider_config_is_a_fail_not_a_crash(tmp_path, serve):
    stub = serve(healthy)
    res = by_id(run_checks(env_for(tmp_path, stub, WIKI_LLM_PROVIDER="custom", WIKI_EMBED_MODEL="off")))
    assert res["llm.config"].level == "FAIL" and "WIKI_LLM_CUSTOM_CONFIG" in res["llm.config"].detail
    res = by_id(run_checks(env_for(tmp_path, stub, WIKI_LLM_PROVIDER="nope", WIKI_EMBED_MODEL="off")))
    assert res["llm.config"].level == "FAIL"


def test_production_checks_and_embedding_endpoint_hint(tmp_path, serve):
    stub = serve(healthy)
    env = env_for(tmp_path, stub, WIKI_EMBED_MODEL="off")
    env.pop("WIKI_ENV")
    res = by_id(run_checks(env))
    assert res["env.secret"].level == "FAIL" and res["env.origins"].level == "FAIL" and res["web.config"].level == "FAIL"
    env.update(WIKI_SESSION_SECRET=SESSION, WIKI_ALLOWED_ORIGINS="https://wiki.corp.test", WIKI_AUTH_PROVIDER="saml")
    res = by_id(run_checks(env))
    assert res["env.secret"].level == "PASS" and res["env.origins"].level == "PASS" and res["web.config"].level == "PASS"
    assert res["auth.provider"].level == "WARN" and "SAML" in res["auth.provider"].title
    assert res["ocr.slot"].level == "WARN"
    # chat on a non-local host, embeddings not given their own endpoint -> hint to point them at LM Studio
    env2 = {"WIKI_ENV": "development", "WIKI_DATA_DIR": str(tmp_path / "d3"), "WIKI_LLM_BASE_URL": "http://gauss.corp.test/v1",
            "WIKI_LLM_ALLOWED_HOSTS": "gauss.corp.test"}
    res = by_id(run_checks(env2, skip_llm=True))
    assert res["embed.endpoint"].level == "WARN" and "WIKI_EMBED_BASE_URL" in res["embed.endpoint"].hint
    assert res["llm.auth"].level == "WARN"  # remote host without a key
    env2["WIKI_EMBED_BASE_URL"] = stub.url + "/v1"
    res = by_id(run_checks(env2, skip_llm=True))
    assert "embed.endpoint" not in res and res["embed.call"].level == "PASS" and "다른 서버" in res["embed.config"].detail


def test_embedding_edge_cases(tmp_path, serve):
    same = serve(lambda r: json_reply({"data": [{"index": 0, "embedding": [1.0, 0.0]}, {"index": 1, "embedding": [1.0, 0.0]}]}))
    r = by_id(run_checks(env_for(tmp_path, same), skip_llm=True))["embed.call"]
    assert r.level == "WARN" and "같은 벡터" in r.title
    zero = serve(lambda r: json_reply({"data": [{"index": 0, "embedding": [0.0, 0.0]}, {"index": 1, "embedding": [0.0, 0.0]}]}))
    r = by_id(run_checks(env_for(tmp_path, zero), skip_llm=True))["embed.call"]
    assert r.level == "FAIL"
    r = by_id(run_checks(env_for(tmp_path, same, WIKI_EMBED_MODEL="off"), skip_llm=True))["embed.call"]
    assert r.level == "WARN" and "비활성" in r.title


def test_report_scrub_is_a_last_line_of_defense():
    from llmwiki.doctor.model import Result

    out = report.scrub([Result("x", "WARN", "t SECRETVALUE", "d SECRETVALUE", "h SECRETVALUE")], ["SECRETVALUE"])[0]
    assert "SECRETVALUE" not in (out.title + out.detail + out.hint)
    assert report.secret_values({"WIKI_LLM_API_KEY": "abcdef", "WIKI_SESSION_SECRET": "x" * 40}) == ["abcdef", "x" * 40]
