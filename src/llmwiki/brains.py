"""Choose the LLM ("brain") per run, or let it switch by itself, without ever sending data to a brain that is not cleared for it.

  WIKI_BRAIN=claude|gemini|gauss|local|<profile name>   use exactly that brain (no fallback)
  WIKI_BRAIN=auto                                       cheapest cleared brain first; on error/quota the next one
  WIKI_BRAIN_DATA=public|personal|company|private       what kind of data this run sends (default company = fail closed)
  WIKI_BRAINS_FILE=config\\brains.json                   the profiles (copy config/brains.example.json)

Profiles (JSON) name a brain and the data classes it may receive. `company` documents, `personal` notes, `private` notes
(diaries...) and `public` material are different classes; a brain that is not listed for a class is never tried for it, not
even as a fallback. Each profile gets an ISOLATED environment (only its own key file, hosts, timeouts): nothing is inherited
from the process, so one brain's key or allowed host can never leak into another. Keys are only ever files (`api_key_file`).
A brain that fails (HTTP error, quota, timeout) rests for `cooldown` seconds, so the next calls do not wait for it again.
A context-too-long error is NOT a reason to switch: it is returned to the caller, who shrinks the prompt.
Nothing here logs prompts or answers; messages carry brain names and error class names only.

  python -m llmwiki.brains list                         profiles, cleared data classes, try order, key file present?
  python -m llmwiki.brains probe NAME                   one tiny real call to that brain (costs one request)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import Settings, load_settings
from .engine.llm import ContextExceeded, LLMError

log = logging.getLogger("llmwiki.brains")
DATA_CLASSES = ("public", "personal", "company", "private")
DEFAULT_FILE = "config/brains.json"
_FIELDS = {"base_url", "model", "cost", "data", "api_key_file", "allowed_hosts", "custom_config", "env"}
_ENV_NAME = re.compile(r"^WIKI_LLM_[A-Z0-9_]+$")
_ENV_DEDICATED = {"WIKI_LLM_API_KEY", "WIKI_LLM_API_KEY_FILE", "WIKI_LLM_BASE_URL", "WIKI_LLM_MODEL", "WIKI_LLM_ALLOWED_HOSTS",
                  "WIKI_LLM_PROVIDER", "WIKI_LLM_CUSTOM_CONFIG"}


class BrainError(Exception):
    """Bad brain configuration or an unusable brain. Messages never contain keys or prompt text."""


class BrainPolicyError(BrainError):
    """No brain is cleared for the requested data class (the request is refused, never rerouted)."""


@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    data: frozenset
    base_url: str = ""
    cost: float = 0.0
    api_key_file: str = ""
    allowed_hosts: tuple = ()
    custom_config: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    order: int = 0


def _path(value: str, base: Path | None) -> str:
    p = Path(value)
    return str(p if p.is_absolute() or base is None else base / p)


def load_profiles(source, base: Path | None = None) -> tuple[dict[str, Profile], str]:
    """(profiles, default choice) from a dict or a JSON file. Relative key/config paths are resolved against `base`
    (default: the folder of the file)."""
    if not isinstance(source, Mapping):
        path = Path(source)
        try:
            source = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise BrainError(f"cannot read the brains file ({type(exc).__name__}): {path}") from None
        base = base or path.parent
    if not isinstance(source, Mapping) or not isinstance(source.get("brains"), Mapping) or not source["brains"]:
        raise BrainError("the brains config needs a non-empty 'brains' object")
    unknown = [k for k in source if k not in ("brains", "default") and not str(k).startswith("_")]
    if unknown:
        raise BrainError(f"unknown top-level key(s): {', '.join(map(str, unknown))}")
    profiles: dict[str, Profile] = {}
    for i, (name, raw) in enumerate(source["brains"].items()):
        if not isinstance(raw, Mapping):
            raise BrainError(f"brain {name!r} must be an object")
        bad = [k for k in raw if k not in _FIELDS and not str(k).startswith("_")]
        if bad:
            raise BrainError(f"brain {name!r}: unknown key(s) {', '.join(map(str, bad))}")
        model, custom = raw.get("model"), raw.get("custom_config") or ""
        if not isinstance(model, str) or not model.strip():
            raise BrainError(f"brain {name!r}: 'model' is required")
        base_url = raw.get("base_url") or ""
        if not custom and not (isinstance(base_url, str) and base_url.strip()):
            raise BrainError(f"brain {name!r}: 'base_url' is required (unless 'custom_config' names the URL)")
        data = raw.get("data")
        if not isinstance(data, list) or not data or any(d not in DATA_CLASSES for d in data):
            raise BrainError(f"brain {name!r}: 'data' must be a non-empty list of {', '.join(DATA_CLASSES)}")
        cost = raw.get("cost", 0)
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise BrainError(f"brain {name!r}: 'cost' must be a number (lower = tried first)")
        hosts = raw.get("allowed_hosts", [])
        if not isinstance(hosts, list) or any(not isinstance(h, str) or not h or "/" in h or ":" in h for h in hosts):
            raise BrainError(f"brain {name!r}: 'allowed_hosts' must be bare host names (no scheme, path or port)")
        env = raw.get("env", {})
        if not isinstance(env, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise BrainError(f"brain {name!r}: 'env' must map names to strings")
        for k in env:
            if not _ENV_NAME.match(k) or k in _ENV_DEDICATED:
                raise BrainError(f"brain {name!r}: env {k!r} is not allowed here (keys/hosts/URL/model have their own fields)")
        key_file = raw.get("api_key_file") or ""
        profiles[name] = Profile(
            name=name, model=model.strip(), data=frozenset(data), base_url=base_url.strip() if isinstance(base_url, str) else "",
            cost=float(cost), api_key_file=_path(key_file, base) if key_file else "", allowed_hosts=tuple(hosts),
            custom_config=_path(custom, base) if custom else "", env=dict(env), order=i)
    default = source.get("default", "auto")
    if default != "auto" and default not in profiles:
        raise BrainError(f"'default' must be 'auto' or one of: {', '.join(profiles)}")
    return profiles, default


def profile_env(p: Profile) -> dict[str, str]:
    """The ONLY environment a brain's client sees. Nothing is inherited from the process."""
    env = {"WIKI_LLM_MODEL": p.model, "WIKI_LLM_PROVIDER": "custom" if p.custom_config else "openai"}
    if p.base_url:
        env["WIKI_LLM_BASE_URL"] = p.base_url
    if p.custom_config:
        env["WIKI_LLM_CUSTOM_CONFIG"] = p.custom_config
    if p.api_key_file:
        env["WIKI_LLM_API_KEY_FILE"] = p.api_key_file
    if p.allowed_hosts:
        env["WIKI_LLM_ALLOWED_HOSTS"] = ",".join(p.allowed_hosts)
    env.update(p.env)
    return env


def _default_factory(p: Profile, settings: Settings):
    from .engine import providers  # lazy: providers imports engine.llm which imports this module lazily

    s = replace(settings, llm_base_url=p.base_url or settings.llm_base_url, llm_model=p.model)
    return providers.llm_from_env(s, profile_env(p))


class BrainRouter:
    """LLMClient look-alike: complete(system, prompt, temperature=None, response_format=None)."""

    def __init__(self, profiles: Mapping[str, Profile], settings: Settings, *, choice: str = "auto", data: str = "company",
                 factory: Callable | None = None, clock: Callable[[], float] = time.monotonic, cooldown: float = 60.0):
        if data not in DATA_CLASSES:
            raise BrainError(f"data class must be one of {', '.join(DATA_CLASSES)}, got {data!r}")
        if choice != "auto" and choice not in profiles:
            raise BrainError(f"unknown brain {choice!r}; choose auto or one of: {', '.join(profiles)}")
        self.profiles, self.settings, self.choice, self.data = dict(profiles), settings, choice, data
        self._factory, self._clock, self.cooldown = factory or _default_factory, clock, cooldown
        self._clients: dict[str, object] = {}
        self._unusable: dict[str, str] = {}
        self._down_until: dict[str, float] = {}
        self.last_used: str | None = None

    def __repr__(self) -> str:
        return f"BrainRouter(choice={self.choice!r}, data={self.data!r}, candidates={self.candidates()})"

    def candidates(self) -> list[str]:
        """Brain names in the order they would be tried for this router's data class."""
        if self.choice != "auto":
            return [self.choice] if self.data in self.profiles[self.choice].data else []
        ok = [p for p in self.profiles.values() if self.data in p.data]
        return [p.name for p in sorted(ok, key=lambda p: (p.cost, p.order))]

    def for_data(self, data: str) -> "BrainRouter":
        """Same brains and caches, another data class (e.g. 'private' for one diary note)."""
        r = BrainRouter(self.profiles, self.settings, choice=self.choice, data=data, factory=self._factory,
                        clock=self._clock, cooldown=self.cooldown)
        r._clients, r._unusable, r._down_until = self._clients, self._unusable, self._down_until
        return r

    def _client(self, name: str):
        if name in self._unusable:
            raise BrainError(f"brain {name!r} is not usable: {self._unusable[name]}")
        if name not in self._clients:
            try:
                self._clients[name] = self._factory(self.profiles[name], self.settings)
            except (ValueError, OSError) as exc:  # ConfigError is a ValueError; messages never carry keys
                self._unusable[name] = f"{type(exc).__name__}: {exc}"
                raise BrainError(f"brain {name!r} is not usable: {self._unusable[name]}") from None
        return self._clients[name]

    def complete(self, system: str, prompt: str, temperature: float | None = None, response_format: dict | None = None) -> str:
        cands = self.candidates()
        if not cands:
            raise BrainPolicyError(f"no brain is cleared for {self.data} data (choice={self.choice}); the request was not sent")
        explicit = self.choice != "auto"
        now = self._clock()
        order = [n for n in cands if self._down_until.get(n, 0.0) <= now] or cands
        failures: list[str] = []
        for name in order:
            try:
                client = self._client(name)
            except BrainError:
                if explicit:
                    raise
                failures.append(f"{name} (unusable)")
                continue
            try:
                out = client.complete(system, prompt, temperature=temperature, response_format=response_format)
            except ContextExceeded:
                raise
            except LLMError as exc:
                self._down_until[name] = self._clock() + self.cooldown
                if explicit:
                    raise
                status = getattr(exc, "status", None)
                failures.append(f"{name} ({type(exc).__name__}{'' if status is None else f' HTTP {status}'})")
                log.warning("brain %s failed (%s); trying the next one", name, type(exc).__name__)
                continue
            if failures:
                log.info("brain switched to %s after: %s", name, "; ".join(failures))
            self.last_used = name
            return out
        raise LLMError("all brains failed: " + "; ".join(failures))


def router_from_env(settings: Settings, environ: Mapping[str, str] | None = None, **kw) -> BrainRouter:
    e = os.environ if environ is None else environ
    choice = (e.get("WIKI_BRAIN") or "").strip()
    if not choice:
        raise BrainError("WIKI_BRAIN is not set")
    profiles, _ = load_profiles((e.get("WIKI_BRAINS_FILE") or "").strip() or DEFAULT_FILE)
    router = BrainRouter(profiles, settings, choice=choice, data=(e.get("WIKI_BRAIN_DATA") or "company").strip().lower(), **kw)
    if not router.candidates():
        raise BrainPolicyError(f"brain {choice!r} is not cleared for {router.data} data; set WIKI_BRAIN_DATA correctly or pick another brain")
    return router


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmwiki.brains", description="Brain profiles (admin/user tool; no document content is sent)")
    ap.add_argument("--file", help=f"brains JSON (default: env WIKI_BRAINS_FILE or {DEFAULT_FILE})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="profiles, cleared data classes, try order; no network")
    pr = sub.add_parser("probe", help="send one tiny request to a brain")
    pr.add_argument("name")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    try:
        profiles, default = load_profiles(args.file or os.environ.get("WIKI_BRAINS_FILE") or DEFAULT_FILE)
        settings = load_settings()
        if args.cmd == "list":
            print(f"default choice in file: {default}   (select with WIKI_BRAIN=<name>|auto, data class with WIKI_BRAIN_DATA)")
            for p in sorted(profiles.values(), key=lambda p: p.order):
                key = "-" if not p.api_key_file else ("key file ok" if Path(p.api_key_file).is_file() else "KEY FILE MISSING")
                print(f"{p.name}\tcost={p.cost:g}\tdata={','.join(c for c in DATA_CLASSES if c in p.data)}\t{key}")
            for d in DATA_CLASSES:
                order = BrainRouter(profiles, settings, data=d).candidates()
                print(f"auto order for {d}: {' > '.join(order) if order else '(no brain is cleared: requests are refused)'}")
            return 0
        if args.name not in profiles:
            raise BrainError(f"unknown brain {args.name!r}; known: {', '.join(profiles)}")
        data = sorted(profiles[args.name].data, key=DATA_CLASSES.index)[0]  # probe with the least sensitive class it accepts
        router = BrainRouter(profiles, settings, choice=args.name, data=data)
        out = router.complete("Reply with the single word: ok", "ping")
        print(f"{args.name}: ok ({len(out)} chars received)")
        return 0
    except LLMError as exc:
        print(f"probe failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    except BrainError as exc:
        print(f"brains error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
