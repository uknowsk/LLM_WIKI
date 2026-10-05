"""One shared local model: serialize every chat/embedding call process-wide, interactive before background, and retry
only transient failures. The proxies keep the clients' call signatures, so QueryService/Compiler/process_file are
constructed with them unchanged. Prompts and replies are never logged here."""
from __future__ import annotations

import heapq
import http.client
import inspect
import itertools
import logging
import threading
import time
import urllib.error
from collections.abc import Callable
from contextlib import contextmanager

from ..engine.llm import ContextExceeded, LLMError

log = logging.getLogger("llmwiki.personal")
INTERACTIVE, BACKGROUND = 0, 1
RETRY_DELAYS = (2.0, 5.0)  # 3 tries in total


def _transient_status(s: int) -> bool:
    return s in (400, 408, 425, 429) or 500 <= s <= 599


class GateTimeout(LLMError):
    """A question waited too long for the shared model (a document is being processed). `busy` lets the web layer
    show the friendly 'try again later' message instead of a generic error."""

    busy = True


class PriorityGate:
    """A mutex whose waiters are served by (priority, arrival order): interactive questions jump the queue."""

    def __init__(self):
        self._cv, self._heap, self._busy = threading.Condition(), [], False
        self._seq = itertools.count()
        self.holder: int | None = None  # priority of the current holder (for the "busy" hint)

    def acquire(self, prio: int, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            ticket = (prio, next(self._seq))
            heapq.heappush(self._heap, ticket)
            try:
                while self._busy or self._heap[0] != ticket:
                    left = None if deadline is None else deadline - time.monotonic()
                    if left is not None and left <= 0:
                        raise GateTimeout("the model is busy (waited too long)")
                    self._cv.wait(left)
            except BaseException:
                self._heap.remove(ticket)
                heapq.heapify(self._heap)
                self._cv.notify_all()
                raise
            heapq.heappop(self._heap)
            self._busy, self.holder = True, prio

    def release(self) -> None:
        with self._cv:
            self._busy, self.holder = False, None
            self._cv.notify_all()

    @contextmanager
    def hold(self, prio: int, timeout: float | None = None):
        self.acquire(prio, timeout)
        try:
            yield
        finally:
            self.release()

    @property
    def background_active(self) -> bool:
        with self._cv:
            return self.holder == BACKGROUND


def _status_of(exc: BaseException) -> int | None:
    for e in _chain(exc):
        for attr in ("status", "code"):
            v = getattr(e, attr, None)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
    return None


def _chain(exc: BaseException) -> list[BaseException]:
    out, seen = [], set()
    while exc is not None and id(exc) not in seen:
        out.append(exc)
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return out


def is_transient(exc: BaseException) -> bool:
    """Connection reset/refused, timeouts, HTTP 5xx/429 and HTTP 400 WITHOUT the context-exceeded signature.
    Never: ContextExceeded, 401/403 (and other 4xx), malformed replies, endpoint-policy errors."""
    if any(isinstance(e, ContextExceeded) for e in _chain(exc)):
        return False
    status = _status_of(exc)
    if status is not None:
        return _transient_status(status)
    return any(isinstance(e, (OSError, http.client.HTTPException, TimeoutError, urllib.error.URLError))
               for e in _chain(exc))


class _Proxy:
    def __init__(self, inner, gate: PriorityGate, prio: int, *, delays: tuple[float, ...] = RETRY_DELAYS,
                 wait: float | None = None,
                 sleep: Callable[[float], object] | None = None, stopped: Callable[[], bool] = lambda: False):
        self._inner, self._gate, self._prio, self._wait = inner, gate, prio, wait
        self._delays, self._sleep, self._stopped = delays, sleep or threading.Event().wait, stopped

    def __getattr__(self, name):  # model, base_url, policy, ... (diagnostics only)
        return getattr(self._inner, name)

    def _call(self, fn: Callable[[], object]):
        attempt = 0
        while True:
            try:
                with self._gate.hold(self._prio, self._wait):  # released while backing off, so a question never waits for a retry
                    return fn()
            except Exception as exc:  # noqa: BLE001
                if (attempt >= len(self._delays) or not is_transient(exc) or self._stopped()
                        or (_status_of(exc) == 400 and attempt >= 1)):  # a bare 400 is retried only once
                    raise
                log.warning("transient model failure (%s), retry %d/%d", type(exc).__name__, attempt + 1, len(self._delays))
                self._sleep(self._delays[attempt])
                attempt += 1


def _accepts(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class SerializedLLM(_Proxy):
    def complete(self, system: str, prompt: str, temperature: float | None = None, response_format: dict | None = None) -> str:
        kw: dict = {}
        if temperature is not None and _accepts(self._inner.complete, "temperature"):
            kw["temperature"] = temperature
        if response_format is not None and _accepts(self._inner.complete, "response_format"):
            kw["response_format"] = response_format
        return self._call(lambda: self._inner.complete(system, prompt, **kw))


class SerializedEmbedder(_Proxy):
    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._call(lambda: self._inner.embed(texts))

    def __getattr__(self, name):
        if name == "embed_query":  # only when the inner embedder has it (retrieval falls back to embed() otherwise)
            inner = self._inner.embed_query
            return lambda text: self._call(lambda: inner(text))
        return super().__getattr__(name)
