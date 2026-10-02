"""Config-driven `custom` embeddings client (WIKI_EMBED_PROVIDER=custom, WIKI_EMBED_CUSTOM_CONFIG=file).

Schema: providers_config.py / config/embed-custom.example.json. Same safety rules as the chat client; input
text is never logged or put into errors, the API key never appears in repr/exception text.
"""
from __future__ import annotations

import json
import math
import os
import urllib.request
import uuid
from collections.abc import Mapping
from urllib.parse import urlparse

from .embed import MAX_CHARS, EmbedError
from .llm import _check_endpoint
from .providers_config import ConfigError, EmbedConfig, load_embed_config, resolve_url
from .providers_http import NetPolicy, Secret, TransportError, build_opener, embed_net, open_request, read_capped
from .providers_template import PathMissing, get_path, has_control_chars, render


class CustomEmbedder:
    def __init__(self, config: EmbedConfig, model: str, *, environ: Mapping[str, str] | None = None,
                 policy: NetPolicy | None = None, api_key: Secret | None = None, query_timeout: float | None = None):
        e = os.environ if environ is None else environ
        self.cfg, self.model = config, model
        self.url = resolve_url(config.url, e, "WIKI_EMBED_CUSTOM_CONFIG")
        if policy is None:
            from .providers import chat_endpoint_url

            policy, key = embed_net(e, self.url, chat_endpoint_url(e.get("WIKI_LLM_BASE_URL") or "http://127.0.0.1:1234/v1", e))
            api_key = api_key if api_key is not None else key
        self.policy, self._key = policy, api_key
        if config.uses_api_key and not self._key:
            raise ConfigError("the embedding config uses {api_key} but no WIKI_EMBED_API_KEY[_FILE] / WIKI_LLM_API_KEY[_FILE] is set")
        _check_endpoint(self.url, self.policy.allowed_hosts)
        self.host = urlparse(self.url).hostname or ""
        self.timeout, self.batch_size = config.timeout, config.batch_size
        try:
            self.query_timeout = float(query_timeout if query_timeout is not None
                                       else e.get("WIKI_EMBED_QUERY_TIMEOUT", "5"))
        except ValueError:
            self.query_timeout = 5.0
        self._opener = build_opener(self.policy)

    def __repr__(self) -> str:
        return f"CustomEmbedder(host={self.host!r}, model={self.model!r}, api_key={'set' if self._key else 'unset'})"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out += self._batch([t[:MAX_CHARS] for t in texts[i:i + self.batch_size]])
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._batch([text[:MAX_CHARS]], self.query_timeout)[0]

    def _request(self, texts: list[str]) -> urllib.request.Request:
        variables = {"model": self.model, "input": texts if self.cfg.input_is_list else texts[0],
                     "uuid": str(uuid.uuid4()), "api_key": self._key.reveal() if self._key else ""}
        headers = {k: str(render(v, variables) or "") for k, v in self.cfg.headers.items()}
        if has_control_chars([*headers.values(), *headers.keys()]):
            raise EmbedError("a rendered header contains control characters (check the key file for stray newlines)")
        if "content-type" not in {k.lower() for k in headers}:
            headers["Content-Type"] = "application/json"
        data = json.dumps(render(self.cfg.body, variables), ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(self.url, data=data, headers=headers, method=self.cfg.method)

    def _batch(self, texts: list[str], timeout: float | None = None) -> list[list[float]]:
        try:
            _check_endpoint(self.url, self.policy.allowed_hosts)
        except ValueError as e:
            raise EmbedError(str(e)) from e
        try:
            with open_request(self._opener, self._request(texts), self.timeout if timeout is None else timeout) as resp:
                raw = read_capped(resp)
        except TransportError as te:
            raise EmbedError(f"embeddings endpoint returned HTTP {te.status}" if te.status else f"embeddings {te}") from te
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise EmbedError("embeddings reply is not valid JSON") from e
        return self._parse(data, len(texts))

    def _parse(self, data, n: int) -> list[list[float]]:
        c = self.cfg
        if c.error_path:
            try:
                if get_path(data, c.error_path):
                    raise EmbedError("embeddings endpoint reported an error")
            except PathMissing:
                pass
        try:
            found = get_path(data, c.vectors_path)
            if not c.input_is_list:
                vecs = [[float(x) for x in found]]
            else:
                if c.index_path:
                    found = sorted(found, key=lambda r: get_path(r, c.index_path))
                vecs = [[float(x) for x in get_path(r, c.vector_item_path)] for r in found]
        except (PathMissing, TypeError, ValueError, KeyError) as e:
            raise EmbedError("embeddings reply has an unexpected shape") from e
        if len(vecs) != n or not vecs[0] or any(len(v) != len(vecs[0]) for v in vecs):
            raise EmbedError("embeddings reply has the wrong number or size of vectors")
        if not all(math.isfinite(x) for v in vecs for x in v):
            raise EmbedError("embeddings reply contains non-finite numbers")
        return vecs


def custom_embedder(model: str, environ: Mapping[str, str] | None = None, **kw) -> CustomEmbedder:
    e = os.environ if environ is None else environ
    path = (e.get("WIKI_EMBED_CUSTOM_CONFIG") or "").strip()
    if not path:
        raise ConfigError("WIKI_EMBED_PROVIDER=custom requires WIKI_EMBED_CUSTOM_CONFIG=<path to JSON config>")
    return CustomEmbedder(load_embed_config(path), model, environ=e, **kw)


def embed_config_url(environ: Mapping[str, str] | None = None) -> str:
    """The URL the custom embedder will call ("" if the config cannot be loaded; custom_embedder reports why)."""
    e = os.environ if environ is None else environ
    try:
        return resolve_url(load_embed_config((e.get("WIKI_EMBED_CUSTOM_CONFIG") or "").strip()).url, e, "WIKI_EMBED_CUSTOM_CONFIG")
    except (ConfigError, ValueError):
        return ""
