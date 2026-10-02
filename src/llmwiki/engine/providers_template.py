"""JSON template substitution and dotted-path extraction for the `custom` providers.

Substitution walks the parsed JSON structure and replaces placeholders ONLY inside string values, in one
regex pass per string, so the inserted text (a document!) is never re-scanned, never parsed as JSON and
never needs escaping: json.dumps of the finished structure does the escaping.

Placeholder grammar inside a string value:  {name}   literal braces: {{ and }}
- If the whole string is exactly one placeholder, the native value is used (number, bool, list, dict).
- A placeholder whose value is None removes that dict key / list item (used for optional fields).
- Otherwise the value is converted to text and spliced in.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

_PH = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")
_OMIT = object()


class PathMissing(LookupError):
    """A dotted path did not resolve. The message names the path only, never any value."""


def placeholders(template: Any) -> set[str]:
    """Every placeholder name used in string values of a JSON template."""
    out: set[str] = set()
    if isinstance(template, str):
        out |= {m.group(1) for m in _PH.finditer(template) if m.group(1)}
    elif isinstance(template, Mapping):
        for v in template.values():
            out |= placeholders(v)
    elif isinstance(template, list):
        for v in template:
            out |= placeholders(v)
    return out


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


def _expand(s: str, variables: Mapping[str, Any]) -> Any:
    m = _PH.fullmatch(s)
    if m and m.group(1):
        v = variables[m.group(1)]
        return _OMIT if v is None else v

    def sub(mm: re.Match) -> str:
        g = mm.group(0)
        if g == "{{":
            return "{"
        if g == "}}":
            return "}"
        return _text(variables[mm.group(1)])

    return _PH.sub(sub, s)


def render(template: Any, variables: Mapping[str, Any]) -> Any:
    """Return a copy of `template` with placeholders in string values replaced (see module docstring)."""
    if isinstance(template, str):
        v = _expand(template, variables)
        return None if v is _OMIT else v
    if isinstance(template, Mapping):
        out = {}
        for k, v in template.items():
            r = _render_item(v, variables)
            if r is not _OMIT:
                out[k] = r
        return out
    if isinstance(template, list):
        return [r for r in (_render_item(v, variables) for v in template) if r is not _OMIT]
    return template


def _render_item(v: Any, variables: Mapping[str, Any]) -> Any:
    if isinstance(v, str):
        return _expand(v, variables)
    return render(v, variables)


def get_path(obj: Any, path: str) -> Any:
    """Dotted path with list indexes: 'choices.0.message.content', 'data.-1.text'. '' returns obj itself."""
    cur = obj
    if not path:
        return cur
    for seg in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                raise PathMissing(f"path {path!r} does not resolve at {seg!r}") from None
        elif isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            raise PathMissing(f"path {path!r} does not resolve at {seg!r}")
    return cur


def has_control_chars(values: Iterable[str]) -> bool:
    return any(any(ord(c) < 32 and c != "\t" or c == "\x7f" for c in v) for v in values)
