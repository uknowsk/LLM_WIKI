"""Provider selection and the config-driven `custom` chat client.

WIKI_LLM_PROVIDER = openai (default: llm.OpenAICompatClient) | custom (this module, WIKI_LLM_CUSTOM_CONFIG=file).
The config schema is documented in providers_config.py and config/llm-custom.example.json. The custom client
keeps the same safety rules as the OpenAI-compatible one (internal hosts only unless explicitly allowed, no
redirects, size caps) and never puts the API key or the prompt into repr(), logs or exception text.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
import time
import urllib.request
import uuid
from collections.abc import Mapping
from urllib.parse import urlparse

from ..config import Settings
from .llm import ContextExceeded, LLMError, OpenAICompatClient, _check_endpoint
from .providers_config import ChatConfig, ConfigError, load_chat_config, resolve_url
from .providers_http import (MAX_RESPONSE, NetPolicy, Secret, TransportError, build_opener, iter_sse_lines, load_secret,
                             open_request, read_capped, strip_think)
from .providers_template import PathMissing, get_path, has_control_chars, render

log = logging.getLogger("llmwiki.llm")
_NO_CONTEXT_CHECK = (401, 403, 404, 429)
_N_CTX = re.compile(r'(?:"n_ctx"\s*:\s*|maximum context length is\s*|context (?:window|size|length)[^0-9]{0,20})(\d{3,})', re.I)


class LLMHTTPError(LLMError):
    """HTTP-level failure. `.status` is the code; `.detail` holds the server message (may echo the prompt:
    never log it; only the doctor, which sends synthetic prompts, prints it)."""

    def __init__(self, message: str, status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status, self.detail = status, detail


def provider_name(environ: Mapping[str, str] | None, var: str) -> str:
    e = os.environ if environ is None else environ
    name = (e.get(var) or "openai").strip().lower() or "openai"
    if name not in ("openai", "custom"):
        raise ValueError(f"{var} must be 'openai' or 'custom', got {name!r}")
    return name


def match_context(texts: list[str], patterns: tuple[str, ...]) -> ContextExceeded | None:
    hay = "\n".join(texts).lower()
    if not any(p in hay for p in patterns):
        return None
    m = _N_CTX.search("\n".join(texts))
    n_ctx = int(m.group(1)) if m else None
    return ContextExceeded(f"LLM context window exceeded (n_ctx={n_ctx})", n_ctx)


def error_texts(body: str, error_path: str) -> list[str]:
    """[server message from error_path (if any), raw body] for matching and diagnostics."""
    out = []
    if error_path:
        try:
            v = get_path(json.loads(body), error_path)
            if v:
                out.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
        except (ValueError, PathMissing):
            pass
    out.append(body)
    return out


class CustomChatClient:
    """Chat client driven by a JSON config (see providers_config). `complete()` matches LLMClient and accepts
    (and, unless supports_json_schema, ignores) `response_format`."""

    def __init__(self, config: ChatConfig, model: str, *, environ: Mapping[str, str] | None = None,
                 timeout: float | None = None, temperature: float = 0.0, api_key: Secret | None = None,
                 policy: NetPolicy | None = None):
        e = os.environ if environ is None else environ
        self.cfg = config
        self.model = model
        self.timeout = float(timeout) if timeout else config.timeout
        self.temperature = temperature
        self._key = api_key if api_key is not None else load_secret(e, "LLM")
        if config.uses_api_key and not self._key:
            raise ConfigError("the config uses {api_key} but neither WIKI_LLM_API_KEY nor WIKI_LLM_API_KEY_FILE is set")
        self.policy = policy or NetPolicy.from_env(e, "LLM")
        self.url = resolve_url(config.url, e, "WIKI_LLM_CUSTOM_CONFIG")
        _check_endpoint(self.url, self.policy.allowed_hosts)
        self.host = urlparse(self.url).hostname or ""
        self._opener = build_opener(self.policy)
        mode = (e.get("WIKI_LLM_STRUCTURED") or "auto").strip().lower() or "auto"
        if mode not in ("auto", "off"):
            raise ValueError(f"WIKI_LLM_STRUCTURED must be 'auto' or 'off', got {mode!r}")
        self._structured_ok = config.supports_json_schema and mode == "auto"
        self.saw_reasoning = False

    @classmethod
    def from_env(cls, settings: Settings, environ: Mapping[str, str] | None = None, **kw) -> "CustomChatClient":
        e = os.environ if environ is None else environ
        return custom_for_model(settings.llm_model, e, **kw)

    def __repr__(self) -> str:
        return (f"CustomChatClient(host={self.host!r}, model={self.model!r}, stream={self.cfg.stream}, "
                f"api_key={'set' if self._key else 'unset'})")

    # --- request -----------------------------------------------------------------------------
    def _build(self, system: str, prompt: str, temperature: float, rf: dict | None) -> urllib.request.Request:
        variables = {
            "model": self.model, "system": system, "prompt": prompt, "temperature": temperature,
            "max_tokens": self.cfg.max_tokens, "stream": self.cfg.stream, "uuid": str(uuid.uuid4()),
            "response_format": rf, "api_key": self._key.reveal() if self._key else "",
        }
        headers = {k: str(render(v, variables) or "") for k, v in self.cfg.headers.items()}
        if has_control_chars([*headers.values(), *headers.keys()]):
            raise LLMError("a rendered header contains control characters (check the key file for stray newlines)")
        low = {k.lower() for k in headers}
        if "content-type" not in low:
            headers["Content-Type"] = "application/json"
        if self.cfg.stream and "accept" not in low:
            headers["Accept"] = "text/event-stream"
        data = json.dumps(render(self.cfg.body, variables), ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(self.url, data=data, headers=headers, method=self.cfg.method)

    def _fail(self, te: TransportError) -> LLMError:
        if te.status is None:
            return LLMError(f"LLM {te}")
        texts = error_texts(te.body, self.cfg.error_path)
        if te.status not in _NO_CONTEXT_CHECK:
            ce = match_context(texts, self.cfg.context_patterns)
            if ce is not None:
                return ce
        return LLMHTTPError(f"LLM endpoint returned HTTP {te.status}", te.status, texts[0][:500])

    def _reply_error(self, data) -> LLMError | None:
        """An error reported inside a 200 JSON reply via error_path."""
        if not self.cfg.error_path or not isinstance(data, (dict, list)):
            return None
        try:
            v = get_path(data, self.cfg.error_path)
        except PathMissing:
            return None
        if not v:
            return None
        msg = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        ce = match_context([msg], self.cfg.context_patterns)
        return ce or LLMHTTPError("LLM reply reported an error", 200, msg[:500])

    def _extract(self, data) -> str:
        err = self._reply_error(data)
        if err is not None:
            raise err
        try:
            content = get_path(data, self.cfg.response_path)
        except PathMissing as e:
            raise LLMError(f"LLM reply has nothing at response_path {self.cfg.response_path!r}") from e
        if not isinstance(content, str):
            raise LLMError("LLM reply content is not text")
        return self._finish(content)

    def _read_stream(self, resp) -> str:
        pieces: list[str] = []
        other: list[str] = []
        saw_data = False
        try:
            for is_data, text in iter_sse_lines(resp, MAX_RESPONSE, time.monotonic() + self.cfg.total_timeout):
                if not is_data:
                    other.append(text)
                    continue
                saw_data = True
                try:
                    ev = json.loads(text)
                except ValueError:
                    continue
                err = self._reply_error(ev)
                if err is not None:
                    raise err
                try:
                    piece = get_path(ev, self.cfg.stream_path)
                except PathMissing:
                    continue  # role-only / usage / keep-alive events carry no text
                if isinstance(piece, str):
                    pieces.append(piece)
        except TransportError as e:
            raise LLMError(f"LLM {e}") from e
        except (OSError, http.client.HTTPException) as e:
            raise LLMError(f"LLM stream interrupted: {type(e).__name__}") from e
        if not saw_data and other and self.cfg.response_path:  # server ignored `stream` and sent plain JSON
            try:
                return self._extract(json.loads("\n".join(other)))
            except ValueError as e:
                raise LLMError("LLM reply is neither an SSE stream nor JSON") from e
        if not pieces:
            raise LLMError(f"LLM stream had no text at stream_path {self.cfg.stream_path!r}")
        return self._finish("".join(pieces))

    def _finish(self, text: str) -> str:
        clean, had, unclosed = strip_think(text)
        self.saw_reasoning = self.saw_reasoning or had
        if unclosed:
            raise LLMError("LLM reply ended inside its reasoning (<think>): raise max_tokens/timeout")
        return clean

    def _send(self, system: str, prompt: str, temperature: float, rf: dict | None) -> str:
        req = self._build(system, prompt, temperature, rf)
        try:
            with open_request(self._opener, req, self.timeout) as resp:
                if self.cfg.stream:
                    return self._read_stream(resp)
                raw = read_capped(resp)
        except TransportError as te:
            raise self._fail(te) from te
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise LLMError("LLM reply is not valid JSON") from e
        return self._extract(data)

    def complete(self, system: str, prompt: str, temperature: float | None = None,
                 response_format: dict | None = None) -> str:
        try:
            _check_endpoint(self.url, self.policy.allowed_hosts)  # DNS answers can change after construction
        except ValueError as e:
            raise LLMError(str(e)) from e
        temp = self.temperature if temperature is None else temperature
        rf = response_format if (response_format is not None and self._structured_ok) else None
        try:
            return self._send(system, prompt, temp, rf)
        except LLMHTTPError as e:
            if rf is None or e.status not in (400, 415, 422, 501):
                raise
            self._structured_ok = False  # server rejects structured output: plain mode from now on
            log.info("LLM server rejected response_format (HTTP %s); using plain mode", e.status)
            return self._send(system, prompt, temp, None)


def custom_for_model(model: str, environ: Mapping[str, str] | None = None, **kw) -> CustomChatClient:
    e = os.environ if environ is None else environ
    path = (e.get("WIKI_LLM_CUSTOM_CONFIG") or "").strip()
    if not path:
        raise ConfigError("WIKI_LLM_PROVIDER=custom requires WIKI_LLM_CUSTOM_CONFIG=<path to JSON config>")
    return CustomChatClient(load_chat_config(path), model, environ=e, **kw)


def chat_endpoint_url(base_url: str, environ: Mapping[str, str] | None = None) -> str:
    """The URL chat requests really go to (custom config url, else the OpenAI-compatible base URL)."""
    e = os.environ if environ is None else environ
    if provider_name(e, "WIKI_LLM_PROVIDER") == "custom":
        try:
            return resolve_url(load_chat_config((e.get("WIKI_LLM_CUSTOM_CONFIG") or "").strip()).url, e, "WIKI_LLM_CUSTOM_CONFIG")
        except (ConfigError, ValueError):
            return ""
    return base_url


def llm_from_env(settings: Settings, environ: Mapping[str, str] | None = None, **kw):
    """The configured chat client. WIKI_LLM_PROVIDER unset/`openai` -> OpenAICompatClient exactly as before."""
    e = os.environ if environ is None else environ
    if provider_name(e, "WIKI_LLM_PROVIDER") == "custom":
        return CustomChatClient.from_env(settings, e, **kw)
    return OpenAICompatClient._plain(settings.llm_base_url, settings.llm_model, environ=e, **kw)
