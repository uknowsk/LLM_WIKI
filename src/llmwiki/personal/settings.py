"""Personal-mode configuration: data home, settings, watch-folder list, bind-host rule. No I/O except reading files."""
from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from ..config import Settings, load_settings

LOOPBACK_HOSTS = ("127.0.0.1", "::1")
SETTINGS_FILE = "settings.json"
LOCAL_EMBED_URL = "http://127.0.0.1:1234/v1"


class PersonalConfigError(ValueError):
    pass


def validate_bind_host(host: str) -> str:
    """Only the literal loopback addresses are accepted: no 0.0.0.0, no hostnames, no whitespace tricks."""
    if host not in LOOPBACK_HOSTS:
        raise PersonalConfigError(f"personal mode binds only to 127.0.0.1 or ::1, not {host!r}")
    return host


def personal_home(environ: Mapping[str, str]) -> Path:
    """WIKI_PERSONAL_HOME, else %LOCALAPPDATA%/LLMWiki (else ~/LLMWiki where LOCALAPPDATA does not exist)."""
    raw = (environ.get("WIKI_PERSONAL_HOME") or "").strip()
    if raw:
        return Path(raw).expanduser().absolute()
    base = (environ.get("LOCALAPPDATA") or "").strip()
    return (Path(base) if base else Path.home()) / "LLMWiki"


def personal_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """Copy of the environment for the engine. The engine reads an explicit dict, never os.environ behind our back.
    Embeddings default to the local LM Studio when the chat endpoint is not on this PC (e.g. the Gauss API)."""
    env = dict(environ)
    chat = (env.get("WIKI_LLM_BASE_URL") or "").strip()
    host = (urlparse(chat).hostname or "") if chat else "127.0.0.1"
    if not (env.get("WIKI_EMBED_BASE_URL") or "").strip() and host not in ("127.0.0.1", "localhost", "::1"):
        env["WIKI_EMBED_BASE_URL"] = LOCAL_EMBED_URL
    return env


def personal_settings(environ: Mapping[str, str], home: Path) -> Settings:
    """Engine settings. env stays 'production' (fail closed); the data dir IS the personal home."""
    base = load_settings(dict(environ))
    return dataclasses.replace(base, env="production", data_dir=home, auth_provider="personal")


def _read_settings_file(home: Path) -> dict:
    p = home / SETTINGS_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise PersonalConfigError(f"{SETTINGS_FILE} is not readable JSON ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise PersonalConfigError(f"{SETTINGS_FILE} must be a JSON object")
    return data


def _split(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("["):
        try:
            items = json.loads(raw)
        except ValueError:
            raise PersonalConfigError("WIKI_PERSONAL_WATCH is not valid JSON") from None
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise PersonalConfigError("WIKI_PERSONAL_WATCH must be a JSON list of strings")
        return items
    return raw.split(";")


def watch_folders(environ: Mapping[str, str], home: Path) -> list[Path]:
    """WIKI_PERSONAL_WATCH (JSON list or ';' separated) wins; else {"watch": [...]} from <home>/settings.json.
    Relative paths are refused (they would depend on the launch directory)."""
    raw = (environ.get("WIKI_PERSONAL_WATCH") or "").strip()
    if raw:
        items = _split(raw)
    else:
        items = _read_settings_file(home).get("watch", [])
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise PersonalConfigError(f'{SETTINGS_FILE}: "watch" must be a list of strings')
    out: list[Path] = []
    for item in items:
        item = item.strip().strip('"')
        if not item:
            continue
        p = Path(os.path.expandvars(item)).expanduser()
        if not p.is_absolute():
            raise PersonalConfigError("watch folders must be absolute paths")
        if p not in out:
            out.append(p)
    return out
