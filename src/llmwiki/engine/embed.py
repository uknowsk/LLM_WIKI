"""Embeddings: Embedder protocol, LM Studio/OpenAI-compatible client, deterministic fake, vector helpers.

Vectors are stored L2-normalized as float32 (native byte order). Text is NEVER logged or put in errors.
"""
from __future__ import annotations

import hashlib
import json
import math
import operator
import os
import urllib.error
import urllib.request
from array import array
from collections.abc import Mapping
from typing import Protocol

from ..config import Settings
from . import article as art
from .llm import _MAX_RESPONSE, _check_endpoint
from .providers_http import NetPolicy, Secret, build_opener, embed_net

DEFAULT_EMBED_MODEL = "text-embedding-bge-m3"
MAX_CHARS = 4000  # inputs are truncated so one long article cannot overflow the model context
_OFF = {"off", "none", "false", "0", "disabled"}
try:  # math.sumprod (3.12+) runs the dot product in C
    _dot = math.sumprod  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover
    def _dot(a, b):
        return sum(map(operator.mul, a, b))


class EmbedError(RuntimeError):
    """The embeddings endpoint failed or returned an unusable reply (message never contains input text)."""


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def embed_text_of(path: str, file_text: str) -> str:
    """The text that gets embedded for an article file: title + body (no frontmatter, no Sources/Related)."""
    a = art.parse(path, file_text)
    return f"{a.title}\n{a.body}"


def text_sha(text: str) -> str:
    return hashlib.sha256(text[:MAX_CHARS].encode("utf-8")).hexdigest()


def normalize(vec) -> array:
    v = array("f", vec)
    n = math.sqrt(_dot(v, v))
    if not math.isfinite(n) or n == 0.0:
        raise EmbedError("embedding has zero or non-finite norm")
    return array("f", (x / n for x in v))


def pack(vec: array) -> bytes:
    return vec.tobytes()


def unpack(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    return a


def cosine(a: array, b: array) -> float:
    """Dot product of two already-normalized vectors."""
    return float(_dot(a, b))


def embed_article(store, embedder: Embedder, path: str, file_text: str) -> str:
    """Embed one article file if its vector is missing/outdated. Returns "ok" | "skipped" | "failed".
    On failure the stale vector is deleted (the article is then BM25-only). Never raises, never logs text."""
    try:
        text = embed_text_of(path, file_text)
        sha = text_sha(text)
        meta = store.embedding_meta([path]).get(path)
        if meta is not None and meta[0] == embedder.model and meta[2] == sha:
            return "skipped"
        vec = normalize(embedder.embed([text[:MAX_CHARS]])[0])
        store.set_embedding(path, embedder.model, len(vec), sha, pack(vec))
        return "ok"
    except Exception:  # noqa: BLE001 (EmbedError, network, bad shape): embeddings are optional
        try:
            store.delete_embedding(path)
        except Exception:  # noqa: BLE001
            pass
        return "failed"


class OpenAICompatEmbedder:
    """POST {base}/embeddings. Same intranet-only rules as OpenAICompatClient: no proxies, no redirects
    (unless opted in via WIKI_EMBED_* / WIKI_LLM_* ALLOWED_HOSTS, PROXY, CA_BUNDLE, API_KEY[_FILE])."""

    def __init__(self, base_url: str, model: str, timeout: float = 60.0, batch_size: int = 16,
                 query_timeout: float | None = None, policy: NetPolicy | None = None, api_key: Secret | None = None):
        if policy is None:  # resolve like the factory: WIKI_LLM_* is inherited only for the same host:port
            from .providers import chat_endpoint_url

            default_chat = os.environ.get("WIKI_LLM_BASE_URL") or "http://127.0.0.1:1234/v1"
            policy, key = embed_net(os.environ, base_url, chat_endpoint_url(default_chat))
            api_key = api_key if api_key is not None else key
        self.policy = policy
        _check_endpoint(base_url, self.policy.allowed_hosts)
        self._key = api_key
        if query_timeout is None:  # short, so a stalled endpoint cannot hold every web query for `timeout`
            try:
                query_timeout = float(os.environ.get("WIKI_EMBED_QUERY_TIMEOUT", "5"))
            except ValueError:
                query_timeout = 5.0
        self.query_timeout = query_timeout
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout
        self.batch_size = max(1, batch_size)
        self._opener = build_opener(self.policy)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out += self._batch([t[:MAX_CHARS] for t in texts[i:i + self.batch_size]])
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._batch([text[:MAX_CHARS]], self.query_timeout)[0]

    def _batch(self, texts: list[str], timeout: float | None = None) -> list[list[float]]:
        try:
            _check_endpoint(self.base_url, self.policy.allowed_hosts)  # DNS answers can change after construction
        except ValueError as e:
            raise EmbedError(str(e)) from e
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = "Bearer " + self._key.reveal()
        req = urllib.request.Request(self.base_url + "/embeddings", data=body, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout if timeout is None else timeout) as resp:  # noqa: S310 (intranet only)
                data = json.loads(resp.read(_MAX_RESPONSE).decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise EmbedError(f"embeddings endpoint returned HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise EmbedError(f"embeddings request failed: {type(e).__name__}") from e
        try:
            rows = sorted(data["data"], key=lambda r: r["index"])
            vecs = [[float(x) for x in r["embedding"]] for r in rows]
        except (KeyError, TypeError, ValueError) as e:
            raise EmbedError("embeddings reply has an unexpected shape") from e
        if len(vecs) != len(texts) or not vecs[0] or any(len(v) != len(vecs[0]) for v in vecs):
            raise EmbedError("embeddings reply has the wrong number or size of vectors")
        if not all(math.isfinite(x) for v in vecs for x in v):
            raise EmbedError("embeddings reply contains non-finite numbers")
        return vecs


class FakeEmbedder:
    """Deterministic test double: hashed bag of character bigrams, L2-normalized. Never touches the network."""

    def __init__(self, dim: int = 256, model: str = "fake-embed"):
        self.dim, self.model = dim, model
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        out = []
        for t in texts:
            t = " ".join(t[:MAX_CHARS].lower().split())
            v = [0.0] * self.dim
            for i in range(max(len(t) - 1, 1)):
                h = int.from_bytes(hashlib.blake2b(t[i:i + 2].encode("utf-8"), digest_size=4).digest(), "big")
                v[h % self.dim] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def embedder_from_env(settings: Settings, environ: Mapping[str, str] | None = None) -> Embedder | None:
    """WIKI_EMBED_MODEL: unset -> text-embedding-bge-m3; "off"/empty -> None (BM25 only).
    WIKI_EMBED_PROVIDER: openai (default; base URL = WIKI_EMBED_BASE_URL, else settings.llm_base_url) | custom
    (WIKI_EMBED_CUSTOM_CONFIG). WIKI_EMBED_ALLOWED_HOSTS/_PROXY/_CA_BUNDLE/_API_KEY[_FILE] fall back to the
    WIKI_LLM_* values ONLY when the embeddings endpoint has the same host:port as the chat endpoint (the chat key
    must never reach a different server): otherwise set WIKI_EMBED_PROXY / WIKI_EMBED_API_KEY explicitly."""
    from .providers import provider_name  # lazy: providers imports this module's siblings

    e = os.environ if environ is None else environ
    raw = e.get("WIKI_EMBED_MODEL")
    model = DEFAULT_EMBED_MODEL if raw is None else raw.strip()
    if not model or model.lower() in _OFF:
        return None
    from .providers import chat_endpoint_url
    from .providers_embed import embed_config_url

    base = (e.get("WIKI_EMBED_BASE_URL") or "").strip() or settings.llm_base_url
    custom = provider_name(e, "WIKI_EMBED_PROVIDER") == "custom"
    policy, key = embed_net(e, embed_config_url(e) if custom else base, chat_endpoint_url(settings.llm_base_url, e))
    if custom:
        from .providers_embed import custom_embedder

        return custom_embedder(model, e, policy=policy, api_key=key)
    return OpenAICompatEmbedder(base, model, policy=policy, api_key=key)
