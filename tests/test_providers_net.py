"""Network policy knobs (allowed hosts, proxy, CA bundle) and the custom embeddings provider."""
from __future__ import annotations

import json
import socket

import pytest

from llmwiki.config import Settings
from llmwiki.engine import llm as llm_mod
from llmwiki.engine.embed import EmbedError, OpenAICompatEmbedder, embedder_from_env
from llmwiki.engine.llm import LLMError, OpenAICompatClient, _check_endpoint, llm_from_env
from llmwiki.engine.providers_config import ConfigError
from llmwiki.engine.providers_embed import CustomEmbedder
from llmwiki.engine.providers_http import NetPolicy
from test_providers_support import OK_REPLY, clean_provider_env, json_reply, serve, write_cfg  # noqa: F401

S = Settings("development", None, "dev", "http://127.0.0.1:1234/v1", "m", False)  # type: ignore[arg-type]
CA_PEM = """-----BEGIN CERTIFICATE-----
MIIBhDCCASugAwIBAgIUW5Wpf5VNkOzSQfmKBHA2xrcYpjwwCgYIKoZIzj0EAwIw
FzEVMBMGA1UEAwwMd2lraS10ZXN0LWNhMCAXDTI2MTAwMjAxMzczMVoYDzIxMjYw
OTA4MDEzNzMxWjAXMRUwEwYDVQQDDAx3aWtpLXRlc3QtY2EwWTATBgcqhkjOPQIB
BggqhkjOPQMBBwNCAARonhfm8oc6F3a6BSHn9JM5JAITcUUh09O/GeMqMP9QfCUc
IrmHC4ouVDUazzcblNHUzstiaN7cabf5C8fw+jaGo1MwUTAdBgNVHQ4EFgQUVIJn
VjxozVYlDptbdf5+vAhSzMkwHwYDVR0jBBgwFoAUVIJnVjxozVYlDptbdf5+vAhS
zMkwDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNHADBEAiEA5lFxclpeTAjC
Y9f+LV/ijEJDaHLD1e76AP86xuSK1SUCHyrFNm1MTfxh6y42AFJCK3Kg6Mt7uyY/
kmHxb7u2v9o=
-----END CERTIFICATE-----
"""


@pytest.fixture()
def public_dns(monkeypatch):
    """Every host name 'resolves' to a public address (no real DNS in tests)."""
    monkeypatch.setattr(llm_mod.socket, "getaddrinfo",
                        lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))])


def test_default_is_strict_and_allowed_hosts_is_exact_opt_in(public_dns):
    with pytest.raises(ValueError, match="intranet"):
        _check_endpoint("https://gauss.corp.test/v1")
    _check_endpoint("https://gauss.corp.test/v1", frozenset({"gauss.corp.test"}))
    with pytest.raises(ValueError, match="intranet"):
        _check_endpoint("https://other.corp.test/v1", frozenset({"gauss.corp.test"}))
    with pytest.raises(ValueError, match="intranet"):
        _check_endpoint("https://sub.gauss.corp.test/v1", frozenset({"gauss.corp.test"}))  # exact, not suffix
    with pytest.raises(ValueError, match="scheme"):
        _check_endpoint("ftp://gauss.corp.test/v1", frozenset({"gauss.corp.test"}))
    with pytest.raises(ValueError, match="non-canonical"):
        _check_endpoint("http://0x08080808/v1", frozenset({"0x08080808"}))


def test_allowed_hosts_env_applies_to_both_clients_and_embed_falls_back(monkeypatch, public_dns):
    with pytest.raises(ValueError):
        OpenAICompatClient("https://gauss.corp.test/v1", "m")
    monkeypatch.setenv("WIKI_LLM_ALLOWED_HOSTS", " Gauss.Corp.Test , other.test ")
    OpenAICompatClient("https://gauss.corp.test/v1", "m")
    with pytest.raises(ValueError):  # different host than the chat endpoint (default 127.0.0.1:1234): no inheritance
        OpenAICompatEmbedder("https://gauss.corp.test/v1", "m")
    monkeypatch.setenv("WIKI_LLM_BASE_URL", "https://gauss.corp.test/v1")
    OpenAICompatEmbedder("https://gauss.corp.test/v1", "m")  # same host as chat: WIKI_EMBED_* falls back to WIKI_LLM_*
    monkeypatch.setenv("WIKI_EMBED_ALLOWED_HOSTS", "emb.test")
    with pytest.raises(ValueError):
        OpenAICompatEmbedder("https://gauss.corp.test/v1", "m")  # explicit embed value wins
    with pytest.raises(ValueError, match="bare host"):
        NetPolicy.from_env({"WIKI_LLM_ALLOWED_HOSTS": "https://x/y"})


def test_proxy_opt_in_routes_through_stub_proxy_and_refuses_redirects(tmp_path, serve, monkeypatch):
    proxy = serve(lambda r: json_reply(OK_REPLY))
    monkeypatch.setenv("WIKI_LLM_ALLOWED_HOSTS", "gauss.corp.test")
    monkeypatch.setenv("WIKI_LLM_PROXY", proxy.url)
    monkeypatch.setenv("WIKI_LLM_API_KEY", "k")
    monkeypatch.setenv("WIKI_LLM_PROVIDER", "custom")
    monkeypatch.setenv("WIKI_LLM_CUSTOM_CONFIG", write_cfg(tmp_path, "http://gauss.corp.test"))
    assert llm_from_env(S).complete("s", "p") == "안녕 OK"
    assert proxy.seen[0]["path"] == "http://gauss.corp.test/chat"  # absolute-form request line = went via proxy
    assert proxy.seen[0]["headers"]["authorization"] == "Bearer k"
    redir = serve(lambda r: (302, {"Location": "http://127.0.0.1:9/x"}, b""))
    monkeypatch.setenv("WIKI_LLM_PROXY", redir.url)
    with pytest.raises(LLMError, match="HTTP 302"):
        llm_from_env(S).complete("s", "p")
    assert len(redir.seen) == 1


def test_proxy_env_vars_are_ignored_by_default_and_proxy_url_validated(serve, monkeypatch):
    dead = serve(lambda r: json_reply({"data": []}))
    monkeypatch.setenv("HTTP_PROXY", dead.url)
    monkeypatch.setenv("http_proxy", dead.url)
    target = serve(lambda r: json_reply(OK_REPLY))
    OpenAICompatClient(target.url + "/v1", "m").complete("s", "p")
    assert len(target.seen) == 1 and not dead.seen
    for bad in ("socks5://h:1", "proxy-only", "http://"):
        with pytest.raises(ValueError, match="PROXY"):
            NetPolicy.from_env({"WIKI_LLM_PROXY": bad})
    assert NetPolicy(proxy="http://user:pw@px.corp:3128").proxy_display() == "http://px.corp:3128"  # no credentials shown


def test_ca_bundle_validation(tmp_path, monkeypatch):
    pem = tmp_path / "ca.pem"
    pem.write_text(CA_PEM)
    monkeypatch.setenv("WIKI_LLM_CA_BUNDLE", str(pem))
    c = OpenAICompatClient("https://127.0.0.1:1/v1", "m")
    assert c.policy.ca_bundle == str(pem) and c.policy.ssl_context() is not None
    OpenAICompatEmbedder("https://127.0.0.1:1/v1", "m", policy=NetPolicy.from_env(None, "EMBED", "LLM"))
    monkeypatch.setenv("WIKI_LLM_CA_BUNDLE", str(tmp_path / "missing.pem"))
    with pytest.raises(ValueError, match="cannot be read"):
        OpenAICompatClient("https://127.0.0.1:1/v1", "m")
    junk = tmp_path / "junk.pem"
    junk.write_text("hello")
    monkeypatch.setenv("WIKI_LLM_CA_BUNDLE", str(junk))
    with pytest.raises(ValueError, match="not a PEM"):
        OpenAICompatClient("https://127.0.0.1:1/v1", "m")
    broken = tmp_path / "broken.pem"
    broken.write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("WIKI_LLM_CA_BUNDLE", str(broken))
    with pytest.raises(ValueError, match="not valid PEM"):
        OpenAICompatClient("https://127.0.0.1:1/v1", "m")


# ---- embeddings -----------------------------------------------------------------------------------------
def embed_cfg(tmp_path, base, **over):
    cfg = {"url": base + "/emb", "headers": {"X-Key": "{api_key}"}, "body": {"model": "{model}", "input": "{input}"},
           "vectors_path": "data", "vector_item_path": "embedding", "index_path": "index", "timeout": 5}
    cfg.update(over)
    p = tmp_path / "embed.json"
    p.write_text(json.dumps({k: v for k, v in cfg.items() if v is not None}), encoding="utf-8")
    return str(p)


def emb_env(monkeypatch, tmp_path, base, embed_key="emb-KEY", **over):
    monkeypatch.setenv("WIKI_EMBED_PROVIDER", "custom")
    monkeypatch.setenv("WIKI_EMBED_CUSTOM_CONFIG", embed_cfg(tmp_path, base, **over))
    monkeypatch.setenv("WIKI_LLM_API_KEY", "llm-KEY")
    if embed_key:
        monkeypatch.setenv("WIKI_EMBED_API_KEY", embed_key)


def test_custom_embedder_batches_sorts_and_inherits_llm_key(tmp_path, serve, monkeypatch):
    def respond(r):
        n = len(r["body"]["input"])
        return json_reply({"data": [{"index": i, "embedding": [float(i + 1), 0.0, 1.0]} for i in reversed(range(n))]})
    stub = serve(respond)
    emb_env(monkeypatch, tmp_path, stub.url, embed_key=None, batch_size=2)
    e = embedder_from_env(Settings("development", None, "dev", stub.url + "/v1", "m", False))  # type: ignore[arg-type]
    assert isinstance(e, CustomEmbedder) and e.model == "text-embedding-bge-m3"
    assert [v[0] for v in e.embed(["a", "b", "c"])] == [1.0, 2.0, 1.0]
    assert [len(r["body"]["input"]) for r in stub.seen] == [2, 1]
    assert stub.seen[0]["headers"]["x-key"] == "llm-KEY"  # embed key defaults to the LLM key
    assert e.embed_query("q") == [1.0, 0.0, 1.0] and "llm-KEY" not in repr(e)


def test_custom_embedder_single_input_mode_and_errors(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply({"vec": [3.0, 4.0], "echo": r["body"]["input"]}))
    emb_env(monkeypatch, tmp_path, stub.url, input_is_list=False, vectors_path="vec", vector_item_path=None, index_path=None)
    e = embedder_from_env(S)
    assert e.embed(["x", "y"]) == [[3.0, 4.0], [3.0, 4.0]] and [r["body"]["input"] for r in stub.seen] == ["x", "y"]
    bad = serve(lambda r: json_reply({"data": [{"index": 0, "embedding": [1.0]}]}))
    emb_env(monkeypatch, tmp_path, bad.url)
    with pytest.raises(EmbedError, match="wrong number"):
        embedder_from_env(S).embed(["a", "b"])
    err = serve(lambda r: json_reply({"error": "SECRETTEXT"}, 500))
    emb_env(monkeypatch, tmp_path, err.url)
    with pytest.raises(EmbedError) as ei:
        embedder_from_env(S).embed(["SECRETTEXT"])
    assert "SECRETTEXT" not in str(ei.value) and "HTTP 500" in str(ei.value)
    nf = serve(lambda r: json_reply({"nothing": 1}))
    emb_env(monkeypatch, tmp_path, nf.url)
    with pytest.raises(EmbedError, match="unexpected shape"):
        embedder_from_env(S).embed(["a"])


def test_embed_custom_requires_config_and_off_still_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_EMBED_PROVIDER", "custom")
    with pytest.raises(ConfigError, match="WIKI_EMBED_CUSTOM_CONFIG"):
        embedder_from_env(S)
    monkeypatch.setenv("WIKI_EMBED_MODEL", "off")
    assert embedder_from_env(S) is None
    monkeypatch.setenv("WIKI_EMBED_PROVIDER", "x")
    monkeypatch.delenv("WIKI_EMBED_MODEL")
    with pytest.raises(ValueError, match="WIKI_EMBED_PROVIDER"):
        embedder_from_env(S)


def test_openai_embedder_key_base_url_and_no_cross_server_inheritance(serve, monkeypatch):
    ok = lambda r: json_reply({"data": [{"index": 0, "embedding": [1.0, 2.0]}]})  # noqa: E731
    chat, emb = serve(ok), serve(ok)
    monkeypatch.setenv("WIKI_LLM_API_KEY", "chat-KEY")
    monkeypatch.setenv("WIKI_LLM_PROXY", chat.url)
    e = embedder_from_env(Settings("development", None, "dev", emb.url + "/v1", "m", False))  # type: ignore[arg-type]
    e.embed(["x"])
    assert chat.seen[0]["headers"]["authorization"] == "Bearer chat-KEY"  # same server: key inherited
    assert chat.seen[0]["path"].startswith("http://") and not emb.seen  # ...and so is the proxy (went via it)
    chat.seen.clear()
    monkeypatch.delenv("WIKI_LLM_PROXY")
    monkeypatch.setenv("WIKI_EMBED_BASE_URL", emb.url + "/v1")
    e2 = embedder_from_env(S)
    assert e2.base_url == emb.url + "/v1"
    e2.embed(["x"])
    assert "authorization" not in emb.seen[-1]["headers"]  # separate server: chat key NOT inherited
    monkeypatch.setenv("WIKI_EMBED_API_KEY", "emb-KEY")
    embedder_from_env(S).embed(["x"])
    assert emb.seen[-1]["headers"]["authorization"] == "Bearer emb-KEY"
    monkeypatch.setenv("WIKI_LLM_PROXY", chat.url)
    e3 = embedder_from_env(S)  # separate base URL: the chat proxy must not capture embed traffic
    e3.embed(["x"])
    assert not chat.seen and emb.seen[-1]["path"].startswith("/v1")


def test_chat_key_is_not_sent_to_a_different_custom_embeddings_host(tmp_path, serve, monkeypatch):
    stub = serve(lambda r: json_reply({"data": [{"index": 0, "embedding": [1.0]}]}))
    emb_env(monkeypatch, tmp_path, stub.url, embed_key=None)
    monkeypatch.setenv("WIKI_LLM_BASE_URL", "http://gauss.corp.test/v1")  # chat lives elsewhere
    with pytest.raises(ConfigError, match="api_key"):  # the template needs a key, only the chat key exists: refuse
        embedder_from_env(S)
    monkeypatch.setenv("WIKI_EMBED_API_KEY", "emb-KEY")
    embedder_from_env(S).embed(["x"])
    assert stub.seen[0]["headers"]["x-key"] == "emb-KEY"
