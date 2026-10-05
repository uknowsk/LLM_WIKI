"""Wires the personal program together: engine + web app + guard + ingest worker, all in one process."""
from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..audit import AuditLog
from ..config import Settings
from ..engine.embed import embedder_from_env
from ..engine.llm import llm_from_env
from ..engine.store import Store
from ..pipeline.ocr_command import ocr_from_env
from ..pipeline.queue import JobQueue
from ..pipeline.run import process_file
from ..web import create_app
from ..web.webconfig import WebConfig
from .guard import LocalOnlyGuard, loopback_origins
from .identity import PersonalAuthProvider, new_launch_token
from .serialize import BACKGROUND, INTERACTIVE, PriorityGate, SerializedEmbedder, SerializedLLM
from .settings import PersonalConfigError, personal_environ, personal_settings, watch_folders
from .status import make_info_handler, make_status_handler
from .watchfolders import WatchIndex, WatchScanner
from .worker import IngestWorker

_UNSET = object()
SESSION_IDLE = 8 * 3600  # a personal browser tab may sit idle for a work day; a restart issues a new token anyway
SESSION_ABSOLUTE = 24 * 3600
DEFAULT_QUESTION_WAIT = 300.0


def question_wait(env: Mapping[str, str]) -> float:
    """WIKI_PERSONAL_QUESTION_WAIT: seconds a question may wait for the shared model (default 300)."""
    raw = (env.get("WIKI_PERSONAL_QUESTION_WAIT") or "").strip()
    try:
        v = float(raw) if raw else DEFAULT_QUESTION_WAIT
    except ValueError:
        raise PersonalConfigError("WIKI_PERSONAL_QUESTION_WAIT must be a number of seconds") from None
    if v <= 0:
        raise PersonalConfigError("WIKI_PERSONAL_QUESTION_WAIT must be positive")
    return v


def ensure_dirs(home: Path) -> None:
    for sub in ("", "inbox", "raw", "wiki"):
        (home / sub).mkdir(parents=True, exist_ok=True)


@dataclass
class Runtime:
    home: Path
    settings: Settings
    wsgi: LocalOnlyGuard
    worker: IngestWorker
    token: str
    gate: PriorityGate
    closers: list[Callable[[], object]]

    def launch_url(self, host: str, port: int) -> str:
        h = f"[{host}]" if ":" in host else host
        return f"http://{h}:{port}/?token={self.token}"

    def close(self) -> None:
        self.worker.stop()
        for fn in reversed(self.closers):
            try:
                fn()
            except Exception:  # noqa: BLE001 (best effort at shutdown)
                pass


def build_runtime(environ: Mapping[str, str], home: Path, port: int, *, llm=_UNSET, embedder=_UNSET,
                  token: str | None = None, sleep: Callable[[float], object] | None = None,
                  retry_delays: tuple[float, ...] | None = None, process=None) -> Runtime:
    """llm/embedder/process can be injected (tests use fakes); by default they come from the WIKI_* variables."""
    env = personal_environ(environ)
    settings = personal_settings(env, home)
    ensure_dirs(home)
    try:
        ocr = ocr_from_env(env)
    except ValueError as exc:
        raise PersonalConfigError(str(exc)) from None
    raw_llm = llm_from_env(settings, env) if llm is _UNSET else llm
    raw_emb = embedder_from_env(settings, env) if embedder is _UNSET else embedder
    gate = PriorityGate()
    kw = {} if retry_delays is None else {"delays": retry_delays}
    wait = question_wait(env)

    def wrap(cls, inner, prio):  # only questions have a bounded wait; background ingest may queue for hours
        limit = None if prio != INTERACTIVE else (min(wait, 30.0) if cls is SerializedEmbedder else wait)
        return None if inner is None else cls(inner, gate, prio, sleep=sleep, wait=limit, **kw)  # (retrieval skips a late embedding)

    store, audit, queue = Store(settings.db_path), AuditLog(settings.db_path), JobQueue(settings)
    token = token or new_launch_token()
    provider = PersonalAuthProvider(token)
    cfg = WebConfig(session_secret=secrets.token_bytes(48), cookie_secure=False, idle_ttl=SESSION_IDLE,
                    absolute_ttl=SESSION_ABSOLUTE, allowed_origins=loopback_origins(port), host="127.0.0.1", port=port,
                    strict_origin=True, personal=True, samesite="Strict",
                    cookie_suffix=f"_{port}", max_threads=16)
    app = create_app(settings, wrap(SerializedLLM, raw_llm, INTERACTIVE), store, audit, provider, config=cfg,
                     embedder=wrap(SerializedEmbedder, raw_emb, INTERACTIVE))
    guard = LocalOnlyGuard(app, provider, port)

    roots = watch_folders(env, home)
    index = WatchIndex(home / "watch_index.db")
    watcher = WatchScanner(home, roots, index) if roots else None
    if process is None:
        extractor = None
        try:
            import pypdf  # noqa: F401
            from ..ingest.pdf import PypdfExtractor
            extractor = PypdfExtractor()
        except ImportError:
            pass  # PDFs then fail per file with a clear message (pip install pypdf)
        bg_llm, bg_emb = wrap(SerializedLLM, raw_llm, BACKGROUND), wrap(SerializedEmbedder, raw_emb, BACKGROUND)

        def process(path, space):
            return process_file(path, space, settings, bg_llm, store, audit, embedder=bg_emb, ocr=ocr,
                                extractor=extractor,
                                # explicit opt-out: personal mode has ONE fixed space for the whole inbox tree
                                verify_folder=False)
    worker = IngestWorker(settings, queue, process, watcher)
    app.add_route("GET", "/api/personal/status", make_status_handler(worker, gate))
    app.add_route("GET", "/api/personal/info", make_info_handler(str(home), [str(r) for r in roots]))
    closers = [store.close, audit.close, queue.close, index.close, app.session_auth.close]
    return Runtime(home, settings, guard, worker, token, gate, closers)
