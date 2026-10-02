"""Default (openai) provider on a reasoning model: Bearer key, structured=off, think stripping, SSE streaming."""
from __future__ import annotations

import time

import pytest

from llmwiki.engine.llm import ContextExceeded, LLMError, OpenAICompatClient
from llmwiki.engine.providers_http import strip_think
from test_providers_support import clean_provider_env, json_reply, serve, sse_reply  # noqa: F401


def reply(content, **msg):
    return {"choices": [{"message": {"content": content, **msg}}]}


def chunk(content=None, **delta):
    return {"choices": [{"delta": ({"content": content} if content is not None else {}) | delta}]}


def mk(stub, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return OpenAICompatClient(stub.url + "/v1", "m")


def test_defaults_send_neither_key_nor_max_tokens_nor_stream(serve, monkeypatch):
    stub = serve(lambda r: json_reply(reply("hi")))
    assert mk(stub, monkeypatch).complete("s", "p") == "hi"
    b = stub.seen[0]["body"]
    assert set(b) == {"model", "temperature", "messages"} and "authorization" not in stub.seen[0]["headers"]


def test_bearer_key_from_file_redacted_everywhere(serve, monkeypatch, tmp_path, caplog):
    kf = tmp_path / "gauss.key"
    kf.write_text("gauss-SECRET-77\n", encoding="utf-8")
    stub = serve(lambda r: json_reply({"error": "echo " + r["headers"]["authorization"]}, 500))
    c = mk(stub, monkeypatch, WIKI_LLM_API_KEY_FILE=kf)
    with caplog.at_level("DEBUG"), pytest.raises(LLMError) as e:
        c.complete("s", "p")
    assert stub.seen[0]["headers"]["authorization"] == "Bearer gauss-SECRET-77"
    for t in (repr(c), str(e.value), repr(e.value), caplog.text, repr(vars(c))):
        assert "gauss-SECRET-77" not in t


def test_structured_off_never_sends_response_format_and_auto_falls_back(serve, monkeypatch):
    def respond(r):
        return json_reply({"error": "unsupported"}, 400) if "response_format" in r["body"] else json_reply(reply("{}"))
    stub = serve(respond)
    rf = {"type": "json_object"}
    off = mk(stub, monkeypatch, WIKI_LLM_STRUCTURED="off")
    off.complete("s", "p", response_format=rf)
    assert len(stub.seen) == 1 and "response_format" not in stub.seen[0]["body"]
    monkeypatch.setenv("WIKI_LLM_STRUCTURED", "auto")
    auto = OpenAICompatClient(stub.url + "/v1", "m")
    auto.complete("s", "p", response_format=rf)  # tries, gets 400, falls back
    auto.complete("s", "p", response_format=rf)  # remembered
    assert [("response_format" in r["body"]) for r in stub.seen[1:]] == [True, False, False]
    monkeypatch.setenv("WIKI_LLM_STRUCTURED", "maybe")
    with pytest.raises(ValueError, match="WIKI_LLM_STRUCTURED"):
        OpenAICompatClient(stub.url + "/v1", "m")


def test_max_tokens_and_timeout_env(serve, monkeypatch):
    stub = serve(lambda r: json_reply(reply("hi")))
    c = mk(stub, monkeypatch, WIKI_LLM_MAX_TOKENS=4096, WIKI_LLM_TIMEOUT=300)
    c.complete("s", "p")
    assert stub.seen[0]["body"]["max_tokens"] == 4096 and c.timeout == 300.0
    assert OpenAICompatClient(stub.url + "/v1", "m", timeout=7).timeout == 7  # explicit argument wins
    for k, v in (("WIKI_LLM_MAX_TOKENS", "abc"), ("WIKI_LLM_MAX_TOKENS", "0"), ("WIKI_LLM_TIMEOUT", "-1"), ("WIKI_LLM_STREAM", "maybe")):
        monkeypatch.setenv(k, v)
        with pytest.raises(ValueError, match=k):
            OpenAICompatClient(stub.url + "/v1", "m")
        monkeypatch.delenv(k)


def test_strip_think_unit():
    assert strip_think("plain") == ("plain", False, False)
    assert strip_think("  <think>a\nb</think>\n\n답") == ("답", True, False)
    assert strip_think("<THINK>x</THINK>ok") == ("ok", True, False)
    assert strip_think("reasoning only</think>답") == ("답", True, False)  # opening tag lives in the chat template
    assert strip_think("<think>never closed") == ("", True, True)
    assert strip_think("1 < 2 and <b>x</b>") == ("1 < 2 and <b>x</b>", False, False)


def test_think_block_and_reasoning_field_never_reach_the_caller(serve, monkeypatch):
    stub = serve(lambda r: json_reply(reply("<think>SECRETTHOUGHT</think>\n\n최종 답", reasoning_content="SECRETTHOUGHT")))
    c = mk(stub, monkeypatch)
    assert c.complete("s", "p") == "최종 답" and c.saw_reasoning
    stub2 = serve(lambda r: json_reply(reply("답", reasoning_content="SECRETTHOUGHT")))
    c2 = mk(stub2, monkeypatch)
    assert c2.complete("s", "p") == "답" and c2.saw_reasoning
    stub3 = serve(lambda r: json_reply(reply("<think>cut off")))
    with pytest.raises(LLMError, match="WIKI_LLM_MAX_TOKENS"):
        mk(stub3, monkeypatch).complete("s", "p")


def test_streaming_accumulates_content_only(serve, monkeypatch):
    ev = [chunk(role="assistant"), chunk(reasoning_content="SECRETTHOUGHT-1"), chunk(None, reasoning_content="SECRETTHOUGHT-2"),
          chunk("안"), {"usage": {"total_tokens": 9}}, chunk("녕"), chunk(" OK"), {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    stub = serve(lambda r: sse_reply(ev))
    c = mk(stub, monkeypatch, WIKI_LLM_STREAM=1, WIKI_LLM_API_KEY="k")
    out = c.complete("s", "p")
    assert out == "안녕 OK" and "SECRET" not in out and c.saw_reasoning
    assert stub.seen[0]["body"]["stream"] is True and stub.seen[0]["headers"]["authorization"] == "Bearer k"
    assert stub.seen[0]["headers"]["accept"] == "text/event-stream"


def test_streaming_with_inline_think_tags(serve, monkeypatch):
    stub = serve(lambda r: sse_reply([chunk("<think>hm"), chunk("m</think>"), chunk("답"), chunk("변")]))
    assert mk(stub, monkeypatch, WIKI_LLM_STREAM=1).complete("s", "p") == "답변"


def test_streaming_reasoning_only_and_empty_are_errors(serve, monkeypatch):
    stub = serve(lambda r: sse_reply([chunk(None, reasoning_content="x")] * 3))
    with pytest.raises(LLMError, match="reasoning only"):
        mk(stub, monkeypatch, WIKI_LLM_STREAM=1).complete("s", "p")
    stub2 = serve(lambda r: sse_reply([]))
    with pytest.raises(LLMError, match="no content"):
        mk(stub2, monkeypatch, WIKI_LLM_STREAM=1).complete("s", "p")


def test_streaming_server_ignores_stream_flag(serve, monkeypatch):
    stub = serve(lambda r: json_reply(reply("<think>t</think>plain")))
    assert mk(stub, monkeypatch, WIKI_LLM_STREAM=1).complete("s", "p") == "plain"


def test_streaming_mid_stream_errors(serve, monkeypatch):
    stub = serve(lambda r: sse_reply([chunk("a"), {"error": {"message": "backend exploded SECRETPROMPT"}}]))
    with pytest.raises(LLMError, match="reported an error") as e:
        mk(stub, monkeypatch, WIKI_LLM_STREAM=1).complete("s", "p")
    assert "SECRETPROMPT" not in str(e.value) and not isinstance(e.value, ContextExceeded)
    ctx = {"error": {"type": "exceed_context_size_error", "n_ctx": 65536, "message": "exceeds the available context size"}}
    stub2 = serve(lambda r: sse_reply([chunk("a"), ctx]))
    with pytest.raises(ContextExceeded) as e2:
        mk(stub2, monkeypatch, WIKI_LLM_STREAM=1).complete("s", "p")
    assert e2.value.n_ctx == 65536


def test_streaming_connection_reset_mid_stream_is_llm_error(serve, monkeypatch):
    import http.client

    from llmwiki.engine import llm as llm_mod

    stub = serve(lambda r: sse_reply([chunk("a")]))
    for exc in (ConnectionResetError("reset"), http.client.IncompleteRead(b"x")):
        def boom(resp, cap):
            yield True, '{"choices":[{"delta":{"content":"a"}}]}'
            raise exc
        monkeypatch.setattr(llm_mod, "iter_sse_lines", boom)
        with pytest.raises(LLMError, match="interrupted"):
            mk(stub, monkeypatch, WIKI_LLM_STREAM=1).complete("s", "p")


def test_http_errors_before_the_stream_starts(serve, monkeypatch):
    body = {"error": {"type": "exceed_context_size_error", "n_ctx": 65536, "message": "exceeds the available context size"}}
    stub = serve(lambda r: json_reply(body, 400))
    with pytest.raises(ContextExceeded) as e:
        mk(stub, monkeypatch, WIKI_LLM_STREAM=1).complete("s", "SECRET PROMPT")
    assert e.value.n_ctx == 65536 and "SECRET PROMPT" not in str(e.value)
    stub2 = serve(lambda r: json_reply({"error": "no"}, 401))
    with pytest.raises(LLMError, match="HTTP 401"):
        mk(stub2, monkeypatch, WIKI_LLM_STREAM=1).complete("s", "p")


def test_timeout_applies_per_read_not_to_the_whole_stream(serve, monkeypatch):
    def slow(r):
        def gen():
            for t in "ABCD":
                time.sleep(0.6)  # every gap < timeout (1s) but the whole answer takes ~2.4s
                yield ("data: " + '{"choices":[{"delta":{"content":"%s"}}]}' % t + "\n\n").encode()
            yield b"data: [DONE]\n\n"
        return 200, {"Content-Type": "text/event-stream"}, gen()
    stub = serve(slow)
    assert mk(stub, monkeypatch, WIKI_LLM_STREAM=1, WIKI_LLM_TIMEOUT=1).complete("s", "p") == "ABCD"


def test_stall_longer_than_timeout_fails(serve, monkeypatch):
    def stall(r):
        def gen():
            yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
            time.sleep(2.5)
            yield b"data: [DONE]\n\n"
        return 200, {"Content-Type": "text/event-stream"}, gen()
    stub = serve(stall)
    with pytest.raises(LLMError, match="interrupted|failed"):
        mk(stub, monkeypatch, WIKI_LLM_STREAM=1, WIKI_LLM_TIMEOUT=1).complete("s", "p")
