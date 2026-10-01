"""Input size limits shared by all parsers. Configurable via WIKI_MAX_INPUT_BYTES or a `max_bytes` argument."""
from __future__ import annotations

import os

DEFAULT_MAX_INPUT_BYTES = 50 * 1024 * 1024


def max_input_bytes(override: int | None = None) -> int:
    if override is not None:
        return override
    try:
        v = int(os.environ.get("WIKI_MAX_INPUT_BYTES", ""))
        return v if v > 0 else DEFAULT_MAX_INPUT_BYTES
    except ValueError:
        return DEFAULT_MAX_INPUT_BYTES


def check_size(data: bytes, source_name: str, max_bytes: int | None = None) -> int:
    """Raise ValueError if `data` exceeds the limit (before any parsing). Returns the limit."""
    limit = max_input_bytes(max_bytes)
    if len(data) > limit:
        raise ValueError(f"input too large ({len(data)} > {limit} bytes): {source_name}")
    return limit
