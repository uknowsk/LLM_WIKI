"""Result model shared by all doctor checks."""
from __future__ import annotations

from dataclasses import asdict, dataclass

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Result:
    id: str
    level: str  # PASS | WARN | FAIL
    title: str
    detail: str = ""
    hint: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def ok(id: str, title: str, detail: str = "") -> Result:
    return Result(id, PASS, title, detail)


def warn(id: str, title: str, detail: str = "", hint: str = "") -> Result:
    return Result(id, WARN, title, detail, hint)


def fail(id: str, title: str, detail: str = "", hint: str = "") -> Result:
    return Result(id, FAIL, title, detail, hint)
