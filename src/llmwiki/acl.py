"""Access rule. A document may be read only if the user holds EVERY space of its sources.

An article compiled from sources in several spaces is therefore visible only to users
who may read all of them (the most restrictive audience), which prevents leakage
through merged wiki pages.
"""
from __future__ import annotations

from collections.abc import Iterable

from .auth import User


def can_read(user: User, doc_spaces: Iterable[str]) -> bool:
    if isinstance(doc_spaces, (str, bytes)):
        raise TypeError("doc_spaces must be an iterable of space names, not a bare string")
    spaces = frozenset(doc_spaces)
    if not spaces:
        return False  # unlabeled documents are never readable (fail closed)
    return spaces <= user.spaces


def filter_readable(user: User, items: Iterable, spaces_of) -> list:
    """Filter items before they reach search results or the LLM context."""
    return [i for i in items if can_read(user, spaces_of(i))]
