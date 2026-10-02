"""LLM access. The model is swappable (dev small model vs prod large model) via Settings only."""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlparse

from ..config import Settings
from .providers_http import (NetPolicy, NoRedirect, Secret, TransportError, build_opener, env_flag, env_number,
                             iter_sse_lines, load_secret, strip_think)

_NUMERIC_HOST = re.compile(r"(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+))*", re.I)  # inet_aton tricks: 0x08080808, 134744072
_MAX_RESPONSE = 8_000_000
log = logging.getLogger("llmwiki.llm")


class LLMError(RuntimeError):
    """The LLM endpoint failed or returned an unusable reply."""


class ContextExceeded(LLMError):
    """The prompt does not fit the model's context window. Never carries prompt text."""

    def __init__(self, message: str, n_ctx: int | None = None):
        super().__init__(message)
        self.n_ctx = n_ctx


def _context_exceeded(e: urllib.error.HTTPError) -> ContextExceeded | None:
    if e.code not in (400, 413):
        return None
    try:
        body = e.read(65536).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    e.wiki_body = body  # type: ignore[attr-defined]  (kept for LLMError.detail; never part of a message)
    if "exceed_context_size_error" not in body and "exceeds the available context" not in body:
        return None
    n_ctx = None
    try:
        v = json.loads(body)["error"]["n_ctx"]
        n_ctx = v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None
    except (ValueError, KeyError, TypeError):
        m = re.search(r'"n_ctx"\s*:\s*(\d+)', body)
        n_ctx = int(m.group(1)) if m else None
    return ContextExceeded(f"LLM context window exceeded (HTTP {e.code}, n_ctx={n_ctx})", n_ctx)


class LLMClient(Protocol):
    def complete(self, system: str, prompt: str, temperature: float | None = None) -> str: ...
    # Implementations may also accept `response_format: dict | None = None` (structured output).


def _internal_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local) and not (ip.is_multicast or ip.is_unspecified)


def _check_endpoint(base_url: str, allowed_hosts: frozenset[str] | tuple = ()) -> None:
    """Fail closed unless the endpoint is http(s) and EVERY address it resolves to is internal.
    `allowed_hosts` (WIKI_LLM_ALLOWED_HOSTS, explicit opt-in) are exact host names exempt from the address check."""
    u = urlparse(base_url)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"LLM endpoint scheme must be http or https, got {u.scheme!r}")
    try:
        host, port = u.hostname or "", u.port
    except ValueError as e:
        raise ValueError(f"invalid LLM endpoint: {e}") from e
    if not host:
        raise ValueError("LLM endpoint has no host")
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        if host.lower() in allowed_hosts and not _NUMERIC_HOST.fullmatch(host):
            return
        if _NUMERIC_HOST.fullmatch(host):
            raise ValueError(f"LLM endpoint host {host!r} is a non-canonical numeric address") from None
        try:
            infos = socket.getaddrinfo(host, port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as e:
            raise ValueError(f"LLM endpoint host {host!r} does not resolve: {e}") from e
        addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
        if not addrs:
            raise ValueError(f"LLM endpoint host {host!r} does not resolve")
    if host.lower() in allowed_hosts:
        return
    if not all(_internal_ip(a) for a in addrs):
        raise ValueError(f"LLM endpoint must be on the intranet, got host {host!r}")


_NoRedirect = NoRedirect  # kept under its old name: embed.py imports it


class OpenAICompatClient:
    """Minimal /chat/completions client (LM Studio, vLLM, llama.cpp, Ollama, OpenAI-compatible gateways such as
    the on-site Gauss API). Refuses non-intranet hosts, ignores proxy environment variables and never follows
    redirects. Env knobs (all optional; unset = behavior as before, strict):
      WIKI_LLM_API_KEY / WIKI_LLM_API_KEY_FILE   Bearer key; sent ONLY when set; never logged or put in errors
      WIKI_LLM_STRUCTURED=auto|off               off: never send response_format (auto: try, fall back on 400)
      WIKI_LLM_MAX_TOKENS=N                      max_tokens in the request (default: not sent). Reasoning
                                                 ("think") models spend output tokens on reasoning: use 4096+
      WIKI_LLM_TIMEOUT=seconds                   default 120; think models: 300+. Per socket read when streaming
      WIKI_LLM_STREAM=1                          SSE streaming (accumulates choices[0].delta.content only)
      WIKI_LLM_ALLOWED_HOSTS / _PROXY / _CA_BUNDLE   network policy opt-ins (see providers_http)
    A leading <think>...</think> block is stripped from replies; `reasoning_content` fields are ignored.
    When WIKI_LLM_PROVIDER=custom, constructing this class returns the config-driven CustomChatClient instead
    (base_url is then ignored; `model`, `timeout`, `temperature` still apply)."""

    def __new__(cls, *args, **kw):
        if cls is OpenAICompatClient:
            from . import providers  # lazy: providers imports this module

            if providers.provider_name(None, "WIKI_LLM_PROVIDER") == "custom":
                model = args[1] if len(args) > 1 else kw.get("model", "")
                return providers.custom_for_model(model, timeout=args[2] if len(args) > 2 else kw.get("timeout"),
                                                  temperature=args[3] if len(args) > 3 else kw.get("temperature", 0.0))
        return super().__new__(cls)

    def __init__(self, base_url: str, model: str, timeout: float | None = None, temperature: float = 0.0,
                 policy: NetPolicy | None = None, api_key: Secret | None = None, environ=None,
                 max_tokens: int | None = None, stream: bool | None = None):
        env = os.environ if environ is None else environ
        self.policy = policy or NetPolicy.from_env(env)
        _check_endpoint(base_url, self.policy.allowed_hosts)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout if timeout is not None else env_number(env, "WIKI_LLM_TIMEOUT", 120.0)
        self.temperature = temperature
        self.max_tokens = max_tokens if max_tokens is not None else env_number(env, "WIKI_LLM_MAX_TOKENS", None, int)
        self.stream = stream if stream is not None else env_flag(env, "WIKI_LLM_STREAM")
        mode = (env.get("WIKI_LLM_STRUCTURED") or "auto").strip().lower() or "auto"
        if mode not in ("auto", "off"):
            raise ValueError(f"WIKI_LLM_STRUCTURED must be 'auto' or 'off', got {mode!r}")
        self._key = api_key if api_key is not None else load_secret(env, "LLM")
        self._opener = build_opener(self.policy)
        self._structured_ok = mode == "auto"  # flips to False once the server rejects response_format
        self.saw_reasoning = False  # True once a reply carried reasoning (<think> or reasoning_content): diagnostics only

    @classmethod
    def _plain(cls, *args, **kw) -> "OpenAICompatClient":
        """Construct the OpenAI-compatible client even when WIKI_LLM_PROVIDER=custom (used by the factory)."""
        obj = object.__new__(cls)
        obj.__init__(*args, **kw)
        return obj

    def __repr__(self) -> str:
        return (f"OpenAICompatClient(base_url={self.base_url!r}, model={self.model!r}, stream={self.stream}, "
                f"api_key={'set' if self._key else 'unset'})")

    def _answer(self, content, reasoning: bool = False) -> str:
        if not isinstance(content, str):
            raise LLMError("LLM reply content is not text")
        clean, had, unclosed = strip_think(content)
        if reasoning or had:
            self.saw_reasoning = True
        if unclosed:
            raise LLMError("LLM reply ended inside its reasoning (<think>): raise WIKI_LLM_MAX_TOKENS and WIKI_LLM_TIMEOUT")
        return clean

    def _post(self, payload: dict) -> str:
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = "Bearer " + self._key.reveal()
        if self.max_tokens:
            payload = {**payload, "max_tokens": self.max_tokens}
        if self.stream:
            payload = {**payload, "stream": True}
            headers["Accept"] = "text/event-stream"
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(payload).encode("utf-8"), headers=headers,
        )
        with self._opener.open(req, timeout=self.timeout) as resp:  # noqa: S310 (intranet only)
            if self.stream:
                return self._read_stream(resp)
            data = json.loads(resp.read(_MAX_RESPONSE).decode("utf-8"))
        return self._from_reply(data)

    def _from_reply(self, data) -> str:
        try:
            msg = data["choices"][0]["message"]
            content = msg["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("LLM reply has no choices[0].message.content") from e
        return self._answer(content, bool(msg.get("reasoning_content") or msg.get("reasoning")))

    def _read_stream(self, resp) -> str:
        parts: list[str] = []
        other: list[str] = []
        reasoning = False
        try:
            for is_data, text in iter_sse_lines(resp, _MAX_RESPONSE):
                if not is_data:
                    other.append(text)
                    continue
                try:
                    ev = json.loads(text)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("error"):
                    raise _stream_error(ev["error"])
                try:
                    delta = ev["choices"][0]["delta"]
                except (KeyError, IndexError, TypeError):
                    continue  # usage / keep-alive chunk
                if delta.get("reasoning_content") or delta.get("reasoning"):
                    reasoning = True  # never forwarded to callers
                if isinstance(delta.get("content"), str):
                    parts.append(delta["content"])
        except TransportError as e:
            raise LLMError(f"LLM {e}") from e
        except (OSError, http.client.HTTPException) as e:  # timeout / reset / truncated stream mid-way
            raise LLMError(f"LLM stream interrupted: {type(e).__name__}") from e
        if not parts and other:  # server ignored `stream` and answered with plain JSON
            try:
                return self._from_reply(json.loads("\n".join(other)))
            except ValueError as e:
                raise LLMError("LLM reply is neither an SSE stream nor JSON") from e
        if not parts:
            raise LLMError("LLM stream ended with reasoning only and no answer text" if reasoning
                           else "LLM stream contained no content")
        return self._answer("".join(parts), reasoning)

    @classmethod
    def from_settings(cls, settings: Settings, **kw) -> "OpenAICompatClient":
        # with WIKI_LLM_PROVIDER=custom, cls(...) returns the CustomChatClient (see __new__)
        return cls(settings.llm_base_url, settings.llm_model, **kw)

    def complete(self, system: str, prompt: str, temperature: float | None = None,
                 response_format: dict | None = None) -> str:
        try:
            _check_endpoint(self.base_url, self.policy.allowed_hosts)  # re-validate: DNS answers can change after construction
        except ValueError as e:
            raise LLMError(str(e)) from e
        payload = {
            "model": self.model,
            "temperature": self.temperature if temperature is None else temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        try:
            if response_format is not None and self._structured_ok:
                try:
                    return self._post({**payload, "response_format": response_format})
                except urllib.error.HTTPError as e:
                    ce = _context_exceeded(e)
                    if ce is not None:
                        raise ce from None
                    if e.code not in (400, 415, 422, 501):
                        raise
                    self._structured_ok = False  # server does not support it: plain mode from now on
                    log.info("LLM server rejected response_format (HTTP %s); using plain mode", e.code)
            return self._post(payload)
        except urllib.error.HTTPError as e:
            ce = _context_exceeded(e)
            if ce is not None:
                raise ce from None
            err = LLMError(f"LLM endpoint returned HTTP {e.code}")
            err.status = e.code  # type: ignore[attr-defined]
            body = getattr(e, "wiki_body", None)
            if body is None:
                try:
                    body = e.read(4096).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    body = ""
            err.detail = " ".join(body.split())[:500]  # type: ignore[attr-defined]  (may echo the prompt: doctor only)
            raise err from e
        except (urllib.error.URLError, OSError, ValueError) as e:  # ValueError covers bad JSON / decode errors
            raise LLMError(f"LLM request failed: {type(e).__name__}: {e}") from e


def _stream_error(err) -> LLMError:
    """An `error` object delivered inside an SSE stream (mid-stream failure). Never carries prompt text."""
    text = json.dumps(err, ensure_ascii=False) if not isinstance(err, str) else err
    if "exceed_context_size_error" in text or "exceeds the available context" in text:
        m = re.search(r'"n_ctx"\s*:\s*(\d+)', text)
        n_ctx = int(m.group(1)) if m else None
        return ContextExceeded(f"LLM context window exceeded (stream, n_ctx={n_ctx})", n_ctx)
    return LLMError("LLM stream reported an error")


def llm_from_env(settings: Settings, environ=None, **kw):
    """Factory: the chat client selected by WIKI_LLM_PROVIDER (openai | custom). See providers.py."""
    from .providers import llm_from_env as _f

    return _f(settings, environ, **kw)


class FakeLLM:
    """Test double. `responder(system, prompt) -> str`; default echoes the prompt so leaks are visible."""

    def __init__(self, responder: Callable[[str, str], str] | None = None):
        self._responder = responder or (lambda system, prompt: prompt)
        self.calls: list[tuple[str, str]] = []
        self.temperatures: list[float | None] = []
        self.response_formats: list[dict | None] = []

    def complete(self, system: str, prompt: str, temperature: float | None = None,
                 response_format: dict | None = None) -> str:
        self.temperatures.append(temperature)
        self.response_formats.append(response_format)
        self.calls.append((system, prompt))
        return self._responder(system, prompt)
