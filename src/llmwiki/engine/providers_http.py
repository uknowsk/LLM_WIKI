"""Shared HTTP plumbing for the LLM / embedding providers: secrets, network policy, transport.

Security model (defaults are STRICT and unchanged unless an env var opts out):
- Only http/https endpoints whose every resolved address is internal are contacted (see llm._check_endpoint).
- WIKI_LLM_ALLOWED_HOSTS: comma list of exact host names that are accepted even when they do not resolve to a
  private address (e.g. an internal API gateway that sits behind a public-looking VIP). RISK: a listed name that
  an attacker can re-point can then receive prompts, so list only names you control.
- WIKI_LLM_PROXY: explicit proxy URL (http://host:port). Default none: proxy environment variables are ignored.
- WIKI_LLM_CA_BUNDLE: PEM file with the internal CA. It REPLACES the default trust store for these calls.
- Redirects are never followed. Responses are size-capped. Keys never appear in repr/log/exception text.
WIKI_EMBED_ALLOWED_HOSTS / _PROXY / _CA_BUNDLE / _API_KEY(_FILE) fall back to the WIKI_LLM_* value when unset.
"""
from __future__ import annotations

import os
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

MAX_RESPONSE = 8_000_000
MAX_ERROR_BODY = 65536


class Secret:
    """Holds an API key. str()/repr() never reveal it; call .reveal() at the moment of use."""

    __slots__ = ("_v",)

    def __init__(self, value: str):
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._v)

    def __reduce__(self):  # never pickle/copy the value out by accident
        raise TypeError("Secret cannot be serialized")


def redact(text: str, *secrets: Secret | None) -> str:
    """Remove every occurrence of the given secrets from text (used before showing server error bodies)."""
    for s in secrets:
        if s is not None and s.reveal():
            text = text.replace(s.reveal(), "***")
    return text


def _get(environ: Mapping[str, str], key: str) -> str:
    return (environ.get(key) or "").strip()


def load_secret(environ: Mapping[str, str] | None, prefix: str = "LLM", fallback: str | None = None) -> Secret | None:
    """WIKI_<prefix>_API_KEY, else the contents of WIKI_<prefix>_API_KEY_FILE (stripped). If neither is set and
    `fallback` is a prefix, that prefix is tried. Raises ValueError (never containing the key) on a bad file."""
    e = os.environ if environ is None else environ
    for p in (prefix, fallback):
        if p is None:
            continue
        val = _get(e, f"WIKI_{p}_API_KEY")
        path = _get(e, f"WIKI_{p}_API_KEY_FILE")
        if not val and path:
            try:
                with open(path, encoding="utf-8-sig") as f:
                    val = f.read(65536).strip()
            except OSError as exc:
                raise ValueError(f"WIKI_{p}_API_KEY_FILE cannot be read ({type(exc).__name__}): {path}") from None
            if not val:
                raise ValueError(f"WIKI_{p}_API_KEY_FILE is empty: {path}")
        if val:
            if any(c in val for c in "\r\n\x00"):
                raise ValueError(f"the API key for WIKI_{p}_* contains control characters (check trailing newlines/quotes)")
            return Secret(val)
    return None


@dataclass(frozen=True)
class NetPolicy:
    allowed_hosts: frozenset[str] = frozenset()
    proxy: str | None = None
    ca_bundle: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, prefix: str = "LLM",
                 fallback: str | None = None, no_inherit: tuple[str, ...] = ()) -> "NetPolicy":
        e = os.environ if environ is None else environ

        def pick(name: str) -> str:
            v = _get(e, f"WIKI_{prefix}_{name}")
            return v if v or fallback is None or name in no_inherit else _get(e, f"WIKI_{fallback}_{name}")

        hosts = frozenset(h.strip().lower() for h in pick("ALLOWED_HOSTS").split(",") if h.strip())
        for h in hosts:
            if "/" in h or ":" in h and not h.startswith("["):
                raise ValueError(f"WIKI_{prefix}_ALLOWED_HOSTS entries must be bare host names, got {h!r}")
        proxy = pick("PROXY") or None
        if proxy:
            u = urlparse(proxy)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise ValueError(f"WIKI_{prefix}_PROXY must look like http://host:port")
        return cls(hosts, proxy, pick("CA_BUNDLE") or None)

    def ssl_context(self) -> ssl.SSLContext | None:
        if not self.ca_bundle:
            return None
        try:
            with open(self.ca_bundle, encoding="ascii", errors="replace") as f:
                if "BEGIN CERTIFICATE" not in f.read(2_000_000):
                    raise ValueError(f"CA bundle is not a PEM file (no BEGIN CERTIFICATE): {self.ca_bundle}")
            return ssl.create_default_context(cafile=self.ca_bundle)
        except ssl.SSLError as exc:  # before OSError: SSLError is an OSError subclass
            raise ValueError(f"CA bundle is not valid PEM ({exc.reason or 'ssl error'}): {self.ca_bundle}") from None
        except OSError as exc:
            raise ValueError(f"CA bundle cannot be read ({type(exc).__name__}): {self.ca_bundle}") from None

    def proxy_display(self) -> str | None:
        if not self.proxy:
            return None
        u = urlparse(self.proxy)
        return f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # urllib then raises HTTPError for the 3xx: a redirect could leave the intranet


def build_opener(policy: NetPolicy | None = None) -> urllib.request.OpenerDirector:
    """Opener without proxy env vars and without redirects; the policy may add an explicit proxy and a CA bundle."""
    p = policy or NetPolicy()
    proxies = {"http": p.proxy, "https": p.proxy} if p.proxy else {}
    handlers: list = [urllib.request.ProxyHandler(proxies), NoRedirect()]
    ctx = p.ssl_context()
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


class TransportError(Exception):
    """A failed HTTP exchange. `body` (server error text, may echo the request) is kept off str()."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status, self.body = status, body


def open_request(opener: urllib.request.OpenerDirector, req: urllib.request.Request, timeout: float):
    """Open `req`; HTTP and network failures become TransportError (cause chain kept for diagnosis)."""
    try:
        return opener.open(req, timeout=timeout)  # noqa: S310 (endpoint validated by the caller)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_ERROR_BODY).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        raise TransportError(f"HTTP {e.code}", e.code, body) from e
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise TransportError(f"request failed: {type(reason).__name__}: {reason}") from e
    except ValueError as e:  # bad URL / header value: do not echo e (may contain a header value)
        raise TransportError("request could not be built (invalid URL or header value)") from e


def read_capped(resp, limit: int = MAX_RESPONSE) -> bytes:
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise TransportError("response larger than the size cap")
    return data


def iter_sse_lines(resp, cap: int = MAX_RESPONSE, deadline: float | None = None, max_line: int = 1_000_000):
    """Yield (True, payload) for each `data:` line and (False, line) for other non-comment lines; stop at
    `[DONE]` or EOF. The socket timeout of `resp` applies PER READ; `deadline` (time.monotonic) is an optional
    overall cap. Raises TransportError when a cap is hit (network errors propagate to the caller)."""
    total = 0
    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise TransportError("stream exceeded total_timeout")
        raw = resp.readline(max_line + 1)
        if not raw:
            return
        total += len(raw)
        if total > cap or len(raw) > max_line:
            raise TransportError("stream larger than the size cap")
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            yield True, payload
        elif line and not line.startswith((":", "event:", "id:", "retry:")):
            yield False, line


_THINK_OPEN = re.compile(r"\s*<think>", re.I)


def strip_think(text: str) -> tuple[str, bool, bool]:
    """Remove a leading <think>...</think> block (reasoning models). Returns (answer, had_reasoning, unclosed).
    Text without reasoning markers is returned untouched. A reply that opens <think> but never closes it
    (generation cut off inside the reasoning) returns ("", True, True)."""
    m = _THINK_OPEN.match(text)
    if m:
        end = text.lower().find("</think>", m.end())
        if end < 0:
            return "", True, True
        return text[end + len("</think>"):].lstrip(), True, False
    end = text.lower().find("</think>")  # some servers drop the opening tag (it is part of the chat template)
    if end >= 0:
        return text[end + len("</think>"):].lstrip(), True, False
    return text, False, False


def env_number(environ: Mapping[str, str], key: str, default, kind=float, minimum: float = 0):
    """Positive number from an env var (None/unset -> default). ValueError names the variable."""
    raw = (environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        v = kind(raw)
    except ValueError:
        raise ValueError(f"{key} must be a number, got {raw!r}") from None
    if v <= minimum:
        raise ValueError(f"{key} must be greater than {minimum:g}")
    return v


def env_flag(environ: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = (environ.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key} must be 1/0 (true/false), got {raw!r}")


def embed_net(environ: Mapping[str, str], embed_url: str, chat_url: str) -> tuple[NetPolicy, Secret | None]:
    """Network policy + API key for the embeddings endpoint. WIKI_EMBED_* is authoritative; the WIKI_LLM_* values
    (key, proxy, allowed hosts, CA bundle) are inherited ONLY when both endpoints have the same host:port, so
    the chat Bearer key can never be sent to a different embeddings server."""
    same = bool(embed_url) and urlparse(embed_url).netloc.lower() == urlparse(chat_url).netloc.lower()
    fb = "LLM" if same else None
    return NetPolicy.from_env(environ, "EMBED", fb), load_secret(environ, "EMBED", fb)
