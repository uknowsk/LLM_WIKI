"""Per-space PII masking policy from a JSON file (WIKI_PII_POLICY_FILE). Fail closed on any problem."""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from llmwiki.ingest.save import validate_space

MAX_POLICY_BYTES = 1024 * 1024


class PiiPolicyError(RuntimeError):
    """Invalid or unreadable policy file: callers must refuse to start (never run unmasked by accident)."""


def _no_dupes(pairs: list[tuple[str, object]]) -> dict:
    d: dict = {}
    for k, v in pairs:
        if k in d:
            raise PiiPolicyError(f"duplicate key {k!r}")
        d[k] = v
    return d


def load_pii_policy(environ: Mapping[str, str] | None = None) -> dict[str, bool] | None:
    """None when WIKI_PII_POLICY_FILE is unset (global WIKI_MASK_PII default applies)."""
    e = os.environ if environ is None else environ
    raw_path = (e.get("WIKI_PII_POLICY_FILE") or "").strip()
    if not raw_path:
        return None
    try:
        p = Path(raw_path)
        if p.stat().st_size > MAX_POLICY_BYTES:
            raise PiiPolicyError("policy file too large")
        data = json.loads(p.read_bytes().decode("utf-8"), object_pairs_hook=_no_dupes)
    except PiiPolicyError:
        raise
    except (OSError, ValueError, RecursionError) as exc:  # includes UnicodeDecodeError, JSONDecodeError
        raise PiiPolicyError(f"WIKI_PII_POLICY_FILE unreadable or not valid JSON ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise PiiPolicyError("PII policy must be a JSON object {space: true|false}")
    for key, val in data.items():
        try:
            validate_space(key)
        except ValueError:
            raise PiiPolicyError(f"unknown/invalid space key {key!r}") from None
        if not isinstance(val, bool):
            raise PiiPolicyError(f"value for {key!r} must be true or false")
    return dict(data)
