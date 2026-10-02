"""Config files for the `custom` chat / embedding providers (JSON; see config/*-custom.example.json).

Chat schema (WIKI_LLM_CUSTOM_CONFIG=path). Keys starting with "_" are comments and ignored; any other unknown
key is an error (a typo must not silently change behavior):
  url                  "https://host/path"  or  {"base_url_env": "WIKI_LLM_BASE_URL", "path": "/chat"}
  method               "POST" (default) or "PUT"
  headers              {name: template}, placeholders allowed ("Bearer {api_key}", "{uuid}")
  body                 JSON template. Placeholders (inside string values only): {model} {system} {prompt}
                       {temperature} {max_tokens} {stream} {response_format} {api_key} {uuid}.
                       A string that is exactly one placeholder keeps its native type ("{temperature}" -> 0.0);
                       a None value (e.g. {response_format} when unused) removes that key.
  response_path        dotted path to the reply text, e.g. "choices.0.message.content"
  error_path           optional dotted path of an error message inside a JSON reply (even on HTTP 200)
  stream               true -> request SSE and join `data: {...}` events; false (default) -> one JSON reply
  stream_path          dotted path of the text piece inside each SSE event, e.g. "choices.0.delta.content"
  context_exceeded_patterns  case-insensitive substrings of the error body/message that mean "prompt too long"
  supports_json_schema true -> {response_format} is filled when the caller asks for structured output
  timeout              seconds per socket operation (default 120)
  total_timeout        overall seconds for one streamed call (default max(timeout*4, 300))
  max_tokens           value for {max_tokens} (default 1024)
Embedding schema (WIKI_EMBED_CUSTOM_CONFIG=path): url, method, headers, body ({model} {input} {api_key} {uuid}),
  vectors_path (list of items, or the single vector when input_is_list=false), vector_item_path (inside each
  item; omit when items are the vectors), index_path (optional sort key inside each item), error_path,
  input_is_list (default true; false -> {input} is one string and batch_size is 1), batch_size (default 16),
  timeout (default 60).
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .providers_template import placeholders

DEFAULT_CONTEXT_PATTERNS = (
    "context_length_exceeded", "exceed_context_size", "context length", "context window", "maximum context",
    "exceeds the available context", "token limit", "too many tokens", "prompt is too long",
)
CHAT_VARS = frozenset({"model", "system", "prompt", "temperature", "max_tokens", "stream", "response_format",
                       "api_key", "uuid"})
EMBED_VARS = frozenset({"model", "input", "api_key", "uuid"})
_CHAT_KEYS = {"url", "method", "headers", "body", "response_path", "error_path", "stream", "stream_path",
              "context_exceeded_patterns", "supports_json_schema", "timeout", "total_timeout", "max_tokens"}
_EMBED_KEYS = {"url", "method", "headers", "body", "vectors_path", "vector_item_path", "index_path", "error_path",
               "input_is_list", "batch_size", "timeout"}


class ConfigError(ValueError):
    """Invalid custom provider config. Messages name keys and files, never secrets or prompt text."""


@dataclass(frozen=True)
class ChatConfig:
    url: Any
    body: Any
    response_path: str
    method: str = "POST"
    headers: Mapping[str, str] = field(default_factory=dict)
    error_path: str = ""
    stream: bool = False
    stream_path: str = ""
    context_patterns: tuple[str, ...] = DEFAULT_CONTEXT_PATTERNS
    supports_json_schema: bool = False
    timeout: float = 120.0
    total_timeout: float = 0.0
    max_tokens: int = 1024

    @property
    def uses_api_key(self) -> bool:
        return "api_key" in placeholders(self.headers) | placeholders(self.body)


@dataclass(frozen=True)
class EmbedConfig:
    url: Any
    body: Any
    vectors_path: str
    method: str = "POST"
    headers: Mapping[str, str] = field(default_factory=dict)
    vector_item_path: str = ""
    index_path: str = ""
    error_path: str = ""
    input_is_list: bool = True
    batch_size: int = 16
    timeout: float = 60.0

    @property
    def uses_api_key(self) -> bool:
        return "api_key" in placeholders(self.headers) | placeholders(self.body)


def _load(path: str, allowed: set[str], what: str) -> dict:
    if not path:
        raise ConfigError(f"{what}: config path is empty")
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = f.read(1_000_000)
    except OSError as e:
        raise ConfigError(f"{what}: cannot read config file {path} ({type(e).__name__})") from None
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ConfigError(f"{what}: {path} is not valid JSON ({e})") from None  # JSONDecodeError shows line/col only
    if not isinstance(data, dict):
        raise ConfigError(f"{what}: {path} must contain a JSON object")
    data = {k: v for k, v in data.items() if not str(k).startswith("_")}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{what}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")
    return data


def _check_common(d: dict, what: str, allowed_vars: frozenset[str]) -> tuple[str, dict]:
    method = str(d.get("method", "POST")).upper()
    if method not in ("POST", "PUT"):
        raise ConfigError(f"{what}: method must be POST or PUT")
    headers = d.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ConfigError(f"{what}: headers must be an object of string -> string")
    if "body" not in d:
        raise ConfigError(f"{what}: 'body' template is required")
    if "url" not in d:
        raise ConfigError(f"{what}: 'url' is required")
    unknown = sorted((placeholders(d["body"]) | placeholders(headers)) - allowed_vars)
    if unknown:
        raise ConfigError(f"{what}: unknown placeholder(s) {unknown}; allowed: {sorted(allowed_vars)}")
    for k in ("timeout", "total_timeout"):
        if k in d and (isinstance(d[k], bool) or not isinstance(d[k], (int, float)) or d[k] <= 0):
            raise ConfigError(f"{what}: {k} must be a positive number")
    return method, headers


def _str(d: dict, key: str, what: str, required: bool = False) -> str:
    v = d.get(key, "")
    if not isinstance(v, str) or (required and not v):
        raise ConfigError(f"{what}: '{key}' must be a {'non-empty ' if required else ''}string")
    return v


def _flag(d: dict, key: str, default: bool, what: str) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ConfigError(f"{what}: '{key}' must be true or false")
    return v


def load_chat_config(path: str) -> ChatConfig:
    what = "WIKI_LLM_CUSTOM_CONFIG"
    d = _load(path, _CHAT_KEYS, what)
    method, headers = _check_common(d, what, CHAT_VARS)
    pats = d.get("context_exceeded_patterns", list(DEFAULT_CONTEXT_PATTERNS))
    if not isinstance(pats, list) or not all(isinstance(p, str) and p for p in pats):
        raise ConfigError(f"{what}: context_exceeded_patterns must be a list of non-empty strings")
    mt = d.get("max_tokens", 1024)
    if isinstance(mt, bool) or not isinstance(mt, int) or mt <= 0:
        raise ConfigError(f"{what}: max_tokens must be a positive integer")
    stream = _flag(d, "stream", False, what)
    stream_path = _str(d, "stream_path", what, required=stream)
    timeout = float(d.get("timeout", 120))
    return ChatConfig(
        url=_check_url(d["url"], what), body=d["body"], method=method, headers=headers,
        response_path=_str(d, "response_path", what, required=not stream), error_path=_str(d, "error_path", what),
        stream=stream, stream_path=stream_path, context_patterns=tuple(p.lower() for p in pats),
        supports_json_schema=_flag(d, "supports_json_schema", False, what), timeout=timeout,
        total_timeout=float(d.get("total_timeout", max(timeout * 4, 300.0))), max_tokens=mt)


def load_embed_config(path: str) -> EmbedConfig:
    what = "WIKI_EMBED_CUSTOM_CONFIG"
    d = _load(path, _EMBED_KEYS, what)
    method, headers = _check_common(d, what, EMBED_VARS)
    bs = d.get("batch_size", 16)
    if isinstance(bs, bool) or not isinstance(bs, int) or bs <= 0:
        raise ConfigError(f"{what}: batch_size must be a positive integer")
    is_list = _flag(d, "input_is_list", True, what)
    return EmbedConfig(
        url=_check_url(d["url"], what), body=d["body"], method=method, headers=headers,
        vectors_path=_str(d, "vectors_path", what, required=True), vector_item_path=_str(d, "vector_item_path", what),
        index_path=_str(d, "index_path", what), error_path=_str(d, "error_path", what), input_is_list=is_list,
        batch_size=bs if is_list else 1, timeout=float(d.get("timeout", 60)))


def _check_url(url: Any, what: str) -> Any:
    if isinstance(url, str) and url.strip():
        return url.strip()
    if isinstance(url, dict) and isinstance(url.get("base_url_env"), str) and url["base_url_env"] \
            and isinstance(url.get("path", ""), str) and set(url) <= {"base_url_env", "path"}:
        return url
    raise ConfigError(f"{what}: url must be a string or {{\"base_url_env\": NAME, \"path\": \"/x\"}}")


def resolve_url(url: Any, environ: Mapping[str, str] | None, what: str) -> str:
    if isinstance(url, str):
        return url
    e = os.environ if environ is None else environ
    name = url["base_url_env"]
    base = (e.get(name) or "").strip()
    if not base:
        raise ConfigError(f"{what}: environment variable {name} (url.base_url_env) is not set")
    path = url.get("path", "")
    return base.rstrip("/") + ("/" + path.lstrip("/") if path else "")
