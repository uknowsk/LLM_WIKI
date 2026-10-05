"""One shared model: no overlapping calls, interactive first, retries only for transient failures."""
import http.client
import inspect
import threading
import time
import urllib.error

import pytest

from llmwiki.engine.embed import EmbedError
from llmwiki.engine.llm import ContextExceeded, FakeLLM, LLMError
from llmwiki.personal.serialize import (BACKGROUND, INTERACTIVE, PriorityGate, SerializedEmbedder, SerializedLLM,
                                        is_transient)


def llm_error(status=None, cause=None):
    e = LLMError(f"LLM endpoint returned HTTP {status}" if status else "LLM request failed")
    if status:
        e.status = status
    if cause is not None:
        e.__cause__ = cause
    return e


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "m", {}, None)


@pytest.mark.parametrize("exc,expected", [
    (ContextExceeded("ctx", 4096), False),
    (llm_error(401), False), (llm_error(403), False), (llm_error(404), False), (llm_error(422), False),
    (llm_error(400), True), (llm_error(500), True), (llm_error(502), True), (llm_error(503), True), (llm_error(429), True),
    (llm_error(cause=urllib.error.URLError(ConnectionRefusedError())), True),
    (llm_error(cause=ConnectionResetError()), True), (llm_error(cause=TimeoutError()), True),
    (llm_error(cause=http.client.IncompleteRead(b"")), True),
    (ConnectionRefusedError(), True), (TimeoutError(), True), (OSError("x"), True),
    (EmbedError("embeddings endpoint returned HTTP 502"), False),  # no status/cause: not classifiable, not retried
    (LLMError("LLM reply has no choices[0].message.content"), False),
    (llm_error(cause=ValueError("bad json")), False),
    (ValueError("x"), False), (RuntimeError("x"), False),
])
def test_transient_classification(exc, expected):
    assert is_transient(exc) is expected


def test_embed_error_classification_follows_its_cause():
    e = EmbedError("embeddings endpoint returned HTTP 502")
    e.__cause__ = http_error(502)
    assert is_transient(e)
    e2 = EmbedError("embeddings endpoint returned HTTP 401")
    e2.__cause__ = http_error(401)
    assert not is_transient(e2)
    e3 = EmbedError("embeddings request failed: URLError")
    e3.__cause__ = urllib.error.URLError("refused")
    assert is_transient(e3)


class Tracker:
    """Chat/embedding double that records overlap and call order."""

    def __init__(self, hold: threading.Event | None = None, delay: float = 0.0):
        self.active = self.max_active = 0
        self.order: list[str] = []
        self.hold, self.delay = hold, delay
        self._lock = threading.Lock()
        self.first_started = threading.Event()

    def complete(self, system, prompt, temperature=None):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.order.append(prompt)
        self.first_started.set()
        try:
            if self.hold is not None and len(self.order) == 1:
                self.hold.wait(5)
            time.sleep(self.delay)
            return "ok"
        finally:
            with self._lock:
                self.active -= 1


def test_calls_never_overlap_across_proxies():
    inner, gate = Tracker(delay=0.01), PriorityGate()
    fg, bg = SerializedLLM(inner, gate, INTERACTIVE), SerializedLLM(inner, gate, BACKGROUND)
    threads = [threading.Thread(target=(fg if i % 2 else bg).complete, args=("s", f"p{i}")) for i in range(12)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert len(inner.order) == 12 and inner.max_active == 1


def _wait_waiting(gate, n):
    for _ in range(500):
        if len(gate._heap) >= n:
            return
        time.sleep(0.01)
    raise AssertionError("waiters did not queue up")


def test_interactive_jumps_ahead_of_queued_background_calls_fifo_otherwise():
    hold = threading.Event()
    inner, gate = Tracker(hold=hold), PriorityGate()
    fg, bg = SerializedLLM(inner, gate, INTERACTIVE), SerializedLLM(inner, gate, BACKGROUND)
    threads = [threading.Thread(target=bg.complete, args=("s", "bg-first"))]
    threads[0].start()
    assert inner.first_started.wait(5)
    for i, (proxy, name) in enumerate([(bg, "bg-1"), (bg, "bg-2"), (fg, "fg-1"), (bg, "bg-3"), (fg, "fg-2")]):
        t = threading.Thread(target=proxy.complete, args=("s", name))
        t.start()
        threads.append(t)
        _wait_waiting(gate, i + 1)
    hold.set()
    [t.join(10) for t in threads]
    assert inner.order == ["bg-first", "fg-1", "fg-2", "bg-1", "bg-2", "bg-3"]
    assert inner.max_active == 1


class Scripted:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def complete(self, system, prompt, temperature=None):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o


def proxy(inner, sleeps):
    return SerializedLLM(inner, PriorityGate(), INTERACTIVE, sleep=sleeps.append)


def test_retries_transient_failures_with_backoff_then_succeeds():
    sleeps = []
    inner = Scripted(llm_error(502), llm_error(503), "answer")
    assert proxy(inner, sleeps).complete("s", "p") == "answer"
    assert inner.calls == 3 and sleeps == [2.0, 5.0]


def test_gives_up_after_three_tries_and_raises_the_original_error():
    sleeps = []
    err = llm_error(cause=ConnectionResetError())
    inner = Scripted(err, err, err, "never")
    with pytest.raises(LLMError) as e:
        proxy(inner, sleeps).complete("s", "p")
    assert e.value is err and inner.calls == 3 and sleeps == [2.0, 5.0]


@pytest.mark.parametrize("exc", [ContextExceeded("ctx", 8192), llm_error(401), llm_error(403), llm_error(404),
                                 LLMError("LLM reply has no choices")])
def test_never_retries_permanent_failures(exc):
    sleeps = []
    inner = Scripted(exc, "never")
    with pytest.raises(LLMError):
        proxy(inner, sleeps).complete("s", "p")
    assert inner.calls == 1 and sleeps == []


def test_gate_is_released_while_backing_off():
    """A question must not wait for a background call's retry delay."""
    gate, release_bg = PriorityGate(), threading.Event()
    bg_inner = Scripted(llm_error(503), "bg-done")
    in_backoff = threading.Event()

    def slow_sleep(seconds):
        in_backoff.set()
        release_bg.wait(5)

    bg = SerializedLLM(bg_inner, gate, BACKGROUND, sleep=slow_sleep)
    fg = SerializedLLM(FakeLLM(lambda s, p: "fg-done"), gate, INTERACTIVE)
    t = threading.Thread(target=bg.complete, args=("s", "p"))
    t.start()
    assert in_backoff.wait(5)
    assert fg.complete("s", "q") == "fg-done"  # would deadlock/timeout if the gate were still held
    release_bg.set()
    t.join(5)


def test_stop_flag_skips_further_retries():
    sleeps, stopped = [], [False]
    inner = Scripted(llm_error(503), "never")
    p = SerializedLLM(inner, PriorityGate(), BACKGROUND, sleep=sleeps.append, stopped=lambda: stopped[0])
    stopped[0] = True
    with pytest.raises(LLMError):
        p.complete("s", "p")
    assert inner.calls == 1


def test_signature_matches_the_engine_contract_and_filters_unsupported_kwargs():
    params = inspect.signature(SerializedLLM.complete).parameters
    assert {"system", "prompt", "temperature", "response_format"} <= set(params)

    class NoRf:
        def complete(self, system, prompt):
            return f"{system}|{prompt}"

    assert SerializedLLM(NoRf(), PriorityGate(), 0).complete("a", "b", temperature=0.3, response_format={"x": 1}) == "a|b"
    fake = FakeLLM()
    SerializedLLM(fake, PriorityGate(), 0).complete("a", "b", 0.2, {"type": "json_schema"})
    assert fake.temperatures == [0.2] and fake.response_formats == [{"type": "json_schema"}]


def test_attribute_passthrough():
    class C:
        model = "m1"

        def complete(self, s, p):
            return ""

    assert SerializedLLM(C(), PriorityGate(), 0).model == "m1"


def test_embedder_proxy_serializes_retries_and_forwards_embed_query_only_if_present():
    class E:
        model = "bge"

        def __init__(self):
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            if self.calls == 1:
                raise EmbedError("x") from urllib.error.URLError("refused")
            return [[1.0, 0.0] for _ in texts]

    sleeps, inner = [], E()
    emb = SerializedEmbedder(inner, PriorityGate(), BACKGROUND, sleep=sleeps.append)
    assert emb.embed(["a", "b"]) == [[1.0, 0.0]] * 2 and sleeps == [2.0] and emb.model == "bge"
    assert getattr(emb, "embed_query", None) is None  # retrieval then falls back to embed()

    class Q(E):
        def embed_query(self, text):
            return [0.0, 1.0]

    assert SerializedEmbedder(Q(), PriorityGate(), INTERACTIVE).embed_query("q") == [0.0, 1.0]
