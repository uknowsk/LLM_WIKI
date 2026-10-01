"""LLM access. The model is swappable (dev small model vs prod large model) via Settings only."""
from __future__ import annotations

import ipaddress
import json
import urllib.request
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlparse

from ..config import Settings

_INTRANET_SUFFIXES = (".local", ".internal", ".intranet", ".lan", ".corp", ".localhost")


class LLMClient(Protocol):
    def complete(self, system: str, prompt: str) -> str: ...


def _is_intranet(host: str) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return "." not in host or host.endswith(_INTRANET_SUFFIXES)


class OpenAICompatClient:
    """Minimal /chat/completions client (vLLM, llama.cpp, Ollama...). Refuses non-intranet hosts."""

    def __init__(self, base_url: str, model: str, timeout: float = 120.0, temperature: float = 0.0):
        host = urlparse(base_url).hostname or ""
        if not _is_intranet(host):
            raise ValueError(f"LLM endpoint must be on the intranet, got host {host!r}")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    @classmethod
    def from_settings(cls, settings: Settings, **kw) -> "OpenAICompatClient":
        return cls(settings.llm_base_url, settings.llm_model, **kw)

    def complete(self, system: str, prompt: str) -> str:
        body = json.dumps({
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (intranet only)
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]


class FakeLLM:
    """Test double. `responder(system, prompt) -> str`; default echoes the prompt so leaks are visible."""

    def __init__(self, responder: Callable[[str, str], str] | None = None):
        self._responder = responder or (lambda system, prompt: prompt)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        return self._responder(system, prompt)
