"""Custom chat provider: templating, parsing, SSE, errors, secrets, factory (loopback stubs only)."""
from __future__ import annotations

import logging

import pytest

from llmwiki.config import Settings
from llmwiki.engine.llm import ContextExceeded, LLMError, OpenAICompatClient, llm_from_env
from llmwiki.engine.providers import CustomChatClient
from llmwiki.engine.providers_config import ConfigError
from llmwiki.engine.providers_template import PathMissing, get_path, placeholders, render
from test_providers_support import OK_REPLY, clean_provider_env, json_reply, serve, sse_reply, write_cfg  # noqa: F401

S = Settings("development", None, "dev", "http://127.0.0.1:1234/v1", "gauss-model", False)  # type: ignore[arg-type]
NASTY = 'He said "hi"\nline2\t\\ {prompt} {api_key} {{x}} 한글   </script> {"a": 1}'


def client(tmp_path, stub, monkeypatch, key="sk-TESTKEY-123", **over):
    if key:
        monkeypatch.setenv("WIKI_LLM_API_KEY", key)
    monkeypatch.setenv("WIKI_LLM_PROVIDER", "custom")
    monkeypatch.setenv("WIKI_LLM_CUSTOM_CONFIG", write_cfg(tmp_path, stub.url, **over))
    return llm_from_env(S)


def test_template_substitution_is_structural_and_safe(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply(OK_REPLY))
    c = client(tmp_path, stub, monkeypatch)
    assert c.complete(NASTY, NASTY, temperature=0.25) == "안녕 OK"
    body = stub.seen[0]["body"]
    assert body["messages"][0]["content"] == NASTY and body["messages"][1]["content"] == NASTY  # exact round trip
    assert "sk-TESTKEY-123" not in stub.seen[0]["raw"].decode()  # {api_key} typed by the user is not expanded
    assert body["model"] == "gauss-model"


def test_non_string_placeholders_keep_their_type(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply(OK_REPLY))
    c = client(tmp_path, stub, monkeypatch, body={"t": "{temperature}", "n": "{max_tokens}", "s": "{stream}",
                                                  "txt": "T={temperature}/{max_tokens}", "lit": "{{raw}}", "rf": "{response_format}"})
    c.complete("s", "p", temperature=0.5)
    b = stub.seen[0]["body"]
    assert b == {"t": 0.5, "n": 1024, "s": False, "txt": "T=0.5/1024", "lit": "{raw}"}  # rf omitted: None


def test_headers_rendered_and_uuid_unique(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply(OK_REPLY))
    c = client(tmp_path, stub, monkeypatch)
    c.complete("s", "p")
    c.complete("s", "p")
    assert stub.seen[0]["headers"]["authorization"] == "Bearer sk-TESTKEY-123"
    assert stub.seen[0]["headers"]["x-request-id"] != stub.seen[1]["headers"]["x-request-id"]
    assert stub.seen[0]["method"] == "POST"


def test_render_and_get_path_units():
    assert placeholders({"a": ["{x}", {"b": "{y} {{z}}"}]}) == {"x", "y"}
    assert render({"a": "{x}", "b": ["{n}", "{none}"]}, {"x": 1, "n": [1, 2], "none": None}) == {"a": 1, "b": [[1, 2]]}
    d = {"a": [{"b": "v"}, {"b": "w"}]}
    assert get_path(d, "a.1.b") == "w" and get_path(d, "a.-1.b") == "w" and get_path(d, "") is d
    for bad in ("a.2.b", "a.x", "z", "a.0.b.c"):
        with pytest.raises(PathMissing):
            get_path(d, bad)


def test_missing_response_path_is_clear_and_leaks_nothing(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply({"other": "SECRETREPLY " + r["body"]["messages"][1]["content"]}))
    c = client(tmp_path, stub, monkeypatch)
    with pytest.raises(LLMError) as e:
        c.complete("s", "PROMPTTEXT")
    assert "response_path" in str(e.value) and "SECRETREPLY" not in str(e.value) and "PROMPTTEXT" not in str(e.value)
    stub2 = serve(lambda r: json_reply({"choices": [{"message": {"content": 5}}]}))
    with pytest.raises(LLMError, match="not text"):
        client(tmp_path, stub2, monkeypatch).complete("s", "p")


def test_bad_json_and_oversize_replies(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: (200, {}, b"<html>PROMPTTEXT</html>"))
    with pytest.raises(LLMError, match="not valid JSON") as e:
        client(tmp_path, stub, monkeypatch).complete("s", "p")
    assert "PROMPTTEXT" not in str(e.value)


def test_error_path_on_http_200_and_on_error_status(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply({"error": {"message": "quota gone"}}))
    c = client(tmp_path, stub, monkeypatch)
    with pytest.raises(LLMError) as e:
        c.complete("s", "p")
    assert not isinstance(e.value, ContextExceeded) and "quota gone" not in str(e.value)
    assert e.value.detail == "quota gone"  # available to the doctor only
    stub2 = serve(lambda r: json_reply({"error": {"message": "boom"}}, 500))
    with pytest.raises(LLMError, match="HTTP 500"):
        client(tmp_path, stub2, monkeypatch).complete("s", "p")


@pytest.mark.parametrize("status,msg,ctx", [
    (400, "This model's Maximum CONTEXT length is 8192 tokens", True),
    (413, "Payload too large: TOKEN LIMIT exceeded", True),
    (500, "internal: context overflow", True),
    (400, "bad request", False),
    (401, "invalid context token", False),
    (429, "rate limit: context", False),
])
def test_context_exceeded_patterns(tmp_path, serve, monkeypatch, status, msg, ctx):
    stub = serve(lambda r: json_reply({"error": {"message": msg}}, status))
    c = client(tmp_path, stub, monkeypatch)
    with pytest.raises(LLMError) as e:
        c.complete("s", "SECRET PROMPT")
    assert isinstance(e.value, ContextExceeded) is ctx and "SECRET PROMPT" not in str(e.value)
    if ctx and "8192" in msg:
        assert e.value.n_ctx == 8192


def test_context_exceeded_on_http_200_error_path(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply({"error": {"message": "Context too long"}}))
    with pytest.raises(ContextExceeded):
        client(tmp_path, stub, monkeypatch).complete("s", "p")


STREAM = dict(stream=True, stream_path="choices.0.delta.content", response_path="choices.0.message.content")


def test_sse_streaming_accumulates_and_ignores_noise(tmp_path, serve, monkeypatch):
    ev = [{"choices": [{"delta": {"role": "assistant"}}]}, {"choices": [{"delta": {"content": "안"}}]},
          "not json", {"usage": {"n": 1}}, {"choices": [{"delta": {"content": "녕 "}}]},
          {"choices": [{"delta": {"content": None}}]}, {"choices": [{"delta": {"content": "OK"}}]}]
    stub = serve(lambda r: sse_reply(ev))
    c = client(tmp_path, stub, monkeypatch, **STREAM, body={"model": "{model}", "stream": "{stream}", "p": "{prompt}"})
    assert c.complete("s", "p") == "안녕 OK"
    assert stub.seen[0]["body"]["stream"] is True and stub.seen[0]["headers"]["accept"] == "text/event-stream"


def test_sse_without_done_comments_and_event_lines(tmp_path, serve, monkeypatch):
    raw = (b": keepalive\n\nevent: message\nid: 1\ndata:{\"choices\":[{\"delta\":{\"content\":\"A\"}}]}\n\n"
           b"data: {\"choices\":[{\"delta\":{\"content\":\"B\"}}]}\r\n\r\n")
    stub = serve(lambda r: (200, {}, raw))
    assert client(tmp_path, stub, monkeypatch, **STREAM).complete("s", "p") == "AB"


def test_stream_requested_but_server_sends_plain_json(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply(OK_REPLY))
    assert client(tmp_path, stub, monkeypatch, **STREAM).complete("s", "p") == "안녕 OK"


def test_stream_errors(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: sse_reply([{"choices": [{"delta": {"x": 1}}]}]))
    with pytest.raises(LLMError, match="no text"):
        client(tmp_path, stub, monkeypatch, **STREAM).complete("s", "p")
    stub2 = serve(lambda r: sse_reply([{"choices": [{"delta": {"content": "a"}}]}, {"error": {"message": "context exceeded"}}]))
    with pytest.raises(ContextExceeded):
        client(tmp_path, stub2, monkeypatch, **STREAM).complete("s", "p")
    stub3 = serve(lambda r: json_reply({"error": {"message": "x context"}}, 400))
    with pytest.raises(ContextExceeded):
        client(tmp_path, stub3, monkeypatch, **STREAM).complete("s", "p")


def test_response_format_ignored_unless_supported(tmp_path, serve, monkeypatch):
    body = {"m": "{prompt}", "response_format": "{response_format}"}
    stub = serve(lambda r: json_reply(OK_REPLY))
    c = client(tmp_path, stub, monkeypatch, body=body)
    c.complete("s", "p", response_format={"type": "json_object"})
    assert "response_format" not in stub.seen[0]["body"]
    c2 = client(tmp_path, stub, monkeypatch, body=body, supports_json_schema=True)
    c2.complete("s", "p", response_format={"type": "json_object"})
    assert stub.seen[1]["body"]["response_format"] == {"type": "json_object"}


def test_structured_output_rejection_falls_back_to_plain(tmp_path, serve, monkeypatch):
    def respond(r):
        return json_reply({"error": {"message": "unsupported field"}}, 400) if "response_format" in r["body"] else json_reply(OK_REPLY)
    stub = serve(respond)
    c = client(tmp_path, stub, monkeypatch, body={"m": "{prompt}", "response_format": "{response_format}"}, supports_json_schema=True)
    assert c.complete("s", "p", response_format={"type": "json_object"}) == "안녕 OK"
    c.complete("s", "p", response_format={"type": "json_object"})
    assert len(stub.seen) == 3 and "response_format" not in stub.seen[2]["body"]  # remembered


def test_triage_style_call_works_with_custom_client(tmp_path, serve, monkeypatch):
    from llmwiki.engine import triage as tg

    stub = serve(lambda r: json_reply({"choices": [{"message": {"content": '{"decision": "New"}'}}]}))
    c = client(tmp_path, stub, monkeypatch)
    assert tg.call_llm(c, "s", "p", response_format=tg.RESPONSE_FORMAT) == '{"decision": "New"}'


def test_redirect_is_refused(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: (302, {"Location": "http://127.0.0.1:9/x"}, b""))
    with pytest.raises(LLMError, match="HTTP 302"):
        client(tmp_path, stub, monkeypatch).complete("s", "p")
    assert len(stub.seen) == 1


def test_key_from_file_and_never_leaked(tmp_path, serve, monkeypatch, caplog):
    kf = tmp_path / "key.txt"
    kf.write_text("  file-KEY-999\r\n", encoding="utf-8")
    stub = serve(lambda r: json_reply({"error": {"message": "echo " + r["headers"]["authorization"]}}, 500))
    monkeypatch.setenv("WIKI_LLM_API_KEY_FILE", str(kf))
    c = client(tmp_path, stub, monkeypatch, key=None)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMError) as e:
            c.complete("s", "p")
    assert stub.seen[0]["headers"]["authorization"] == "Bearer file-KEY-999"
    for text in (repr(c), str(e.value), repr(e.value), caplog.text, repr(c._key), str(c._key), f"{c._key}", repr(vars(c))):
        assert "file-KEY-999" not in text
    assert "file-KEY-999" not in repr(c.cfg)


def test_key_env_beats_file_and_bad_key_inputs(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply(OK_REPLY))
    kf = tmp_path / "k.txt"
    kf.write_text("from-file", encoding="utf-8")
    monkeypatch.setenv("WIKI_LLM_API_KEY_FILE", str(kf))
    client(tmp_path, stub, monkeypatch, key="from-env").complete("s", "p")
    assert stub.seen[0]["headers"]["authorization"] == "Bearer from-env"
    monkeypatch.setenv("WIKI_LLM_API_KEY", "bad\nkey")
    with pytest.raises(ValueError) as e:
        client(tmp_path, stub, monkeypatch, key=None)
    assert "bad" not in str(e.value)
    monkeypatch.delenv("WIKI_LLM_API_KEY")
    monkeypatch.setenv("WIKI_LLM_API_KEY_FILE", str(tmp_path / "missing.txt"))
    with pytest.raises(ValueError, match="cannot be read"):
        client(tmp_path, stub, monkeypatch, key=None)


def test_missing_key_when_template_needs_it(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply(OK_REPLY))
    with pytest.raises(ConfigError, match="api_key"):
        client(tmp_path, stub, monkeypatch, key=None)
    client(tmp_path, stub, monkeypatch, key=None, headers={})  # no {api_key} in the config: fine


@pytest.mark.parametrize("over,msg", [
    ({"bogus": 1}, "unknown key"),
    ({"body": {"x": "{nope}"}}, "unknown placeholder"),
    ({"method": "DELETE"}, "method"),
    ({"stream": True, "stream_path": None}, "stream_path"),
    ({"response_path": None}, "response_path"),
    ({"timeout": -1}, "timeout"),
    ({"context_exceeded_patterns": "x"}, "context_exceeded_patterns"),
    ({"headers": {"A": 1}}, "headers"),
    ({"url": 5}, "url"),
])
def test_config_validation(tmp_path, monkeypatch, over, msg):
    monkeypatch.setenv("WIKI_LLM_PROVIDER", "custom")
    monkeypatch.setenv("WIKI_LLM_API_KEY", "k")
    monkeypatch.setenv("WIKI_LLM_CUSTOM_CONFIG", write_cfg(tmp_path, "http://127.0.0.1:9", **over))
    with pytest.raises(ConfigError, match=msg):
        llm_from_env(S)


def test_config_file_problems(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_LLM_PROVIDER", "custom")
    with pytest.raises(ConfigError, match="WIKI_LLM_CUSTOM_CONFIG"):
        llm_from_env(S)
    monkeypatch.setenv("WIKI_LLM_CUSTOM_CONFIG", str(tmp_path / "nope.json"))
    with pytest.raises(ConfigError, match="cannot read"):
        llm_from_env(S)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("WIKI_LLM_CUSTOM_CONFIG", str(bad))
    with pytest.raises(ConfigError, match="not valid JSON"):
        llm_from_env(S)


def test_url_from_env_var_and_path(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply(OK_REPLY))
    monkeypatch.setenv("WIKI_LLM_BASE_URL", stub.url + "/")
    c = client(tmp_path, stub, monkeypatch, url={"base_url_env": "WIKI_LLM_BASE_URL", "path": "/gauss/chat"})
    c.complete("s", "p")
    assert stub.seen[0]["path"] == "/gauss/chat"
    monkeypatch.delenv("WIKI_LLM_BASE_URL")
    with pytest.raises(ConfigError, match="WIKI_LLM_BASE_URL"):
        client(tmp_path, stub, monkeypatch, url={"base_url_env": "WIKI_LLM_BASE_URL", "path": "/gauss/chat"})


def test_factory_selection_and_delegation(tmp_path, serve, monkeypatch):
    assert type(llm_from_env(S)) is OpenAICompatClient
    monkeypatch.setenv("WIKI_LLM_PROVIDER", " OpenAI ")
    assert type(llm_from_env(S)) is OpenAICompatClient
    monkeypatch.setenv("WIKI_LLM_PROVIDER", "gauss")
    with pytest.raises(ValueError, match="WIKI_LLM_PROVIDER"):
        llm_from_env(S)
    stub = serve(lambda r: json_reply(OK_REPLY))
    c = client(tmp_path, stub, monkeypatch)
    assert isinstance(c, CustomChatClient)
    # every existing entry point (web/pipeline: from_settings, eval: direct construction) follows the env
    assert isinstance(OpenAICompatClient.from_settings(S), CustomChatClient)
    d = OpenAICompatClient("http://127.0.0.1:1234/v1", "m2", timeout=7)
    assert isinstance(d, CustomChatClient) and d.timeout == 7 and d.model == "m2"


def test_openai_client_sends_key_only_when_configured(serve, monkeypatch):
    stub = serve(lambda r: json_reply(OK_REPLY))
    base = stub.url + "/v1"
    assert OpenAICompatClient(base, "m").complete("s", "p") == "안녕 OK"
    assert "authorization" not in stub.seen[0]["headers"]
    monkeypatch.setenv("WIKI_LLM_API_KEY", "oa-KEY")
    c = OpenAICompatClient(base, "m")
    c.complete("s", "p")
    assert stub.seen[1]["headers"]["authorization"] == "Bearer oa-KEY" and "oa-KEY" not in repr(c)
