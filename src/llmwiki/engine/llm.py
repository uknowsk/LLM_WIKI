"""LLM access. The model is swappable (dev small model vs prod large model) via Settings only."""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlparse

from ..config import Settings

_NUMERIC_HOST = re.compile(r"(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+))*", re.I)  # inet_aton tricks: 0x08080808, 134744072
_MAX_RESPONSE = 8_000_000
log = logging.getLogger("llmwiki.llm")


class LLMError(RuntimeError):
    """The LLM endpoint failed or returned an unusable reply."""


class LLMClient(Protocol):
    def complete(self, system: str, prompt: str, temperature: float | None = None) -> str: ...
    # Implementations may also accept `response_format: dict | None = None` (structured output).


def _internal_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local) and not (ip.is_multicast or ip.is_unspecified)


def _check_endpoint(base_url: str) -> None:
    """Fail closed unless the endpoint is http(s) and EVERY address it resolves to is internal."""
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
        if _NUMERIC_HOST.fullmatch(host):
            raise ValueError(f"LLM endpoint host {host!r} is a non-canonical numeric address") from None
        try:
            infos = socket.getaddrinfo(host, port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as e:
            raise ValueError(f"LLM endpoint host {host!r} does not resolve: {e}") from e
        addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
        if not addrs:
            raise ValueError(f"LLM endpoint host {host!r} does not resolve")
    if not all(_internal_ip(a) for a in addrs):
        raise ValueError(f"LLM endpoint must be on the intranet, got host {host!r}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # urllib then raises HTTPError for the 3xx: a redirect could leave the intranet


class OpenAICompatClient:
    """Minimal /chat/completions client (vLLM, llama.cpp, Ollama...). Refuses non-intranet hosts,
    ignores proxy environment variables and never follows redirects."""

    def __init__(self, base_url: str, model: str, timeout: float = 120.0, temperature: float = 0.0):
        _check_endpoint(base_url)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._structured_ok = True  # flips to False once the server rejects response_format

    def _post(self, payload: dict) -> str:
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with self._opener.open(req, timeout=self.timeout) as resp:  # noqa: S310 (intranet only)
            data = json.loads(resp.read(_MAX_RESPONSE).decode("utf-8"))
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("LLM reply has no choices[0].message.content") from e
        if not isinstance(content, str):
            raise LLMError("LLM reply content is not text")
        return content

    @classmethod
    def from_settings(cls, settings: Settings, **kw) -> "OpenAICompatClient":
        return cls(settings.llm_base_url, settings.llm_model, **kw)

    def complete(self, system: str, prompt: str, temperature: float | None = None,
                 response_format: dict | None = None) -> str:
        try:
            _check_endpoint(self.base_url)  # re-validate: DNS answers can change after construction
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
                    if e.code not in (400, 415, 422, 501):
                        raise
                    self._structured_ok = False  # server does not support it: plain mode from now on
                    log.info("LLM server rejected response_format (HTTP %s); using plain mode", e.code)
            return self._post(payload)
        except urllib.error.HTTPError as e:
            raise LLMError(f"LLM endpoint returned HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:  # ValueError covers bad JSON / decode errors
            raise LLMError(f"LLM request failed: {type(e).__name__}: {e}") from e


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
