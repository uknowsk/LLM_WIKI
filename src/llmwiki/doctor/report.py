"""Render doctor results (Korean text or JSON) with secrets scrubbed."""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from ..engine.providers_http import load_secret
from .model import FAIL, PASS, WARN, Result

_MARK = {PASS: "[PASS]", WARN: "[WARN]", FAIL: "[FAIL]"}


def secret_values(environ: Mapping[str, str]) -> list[str]:
    """Every secret the process could hold: used to scrub the final output as a last line of defense."""
    vals: list[str] = []
    for prefix in ("LLM", "EMBED"):
        try:
            s = load_secret(environ, prefix)
        except ValueError:
            s = None
        if s is not None:
            vals.append(s.reveal())
    sess = (environ.get("WIKI_SESSION_SECRET") or "").strip()
    if sess:
        vals.append(sess)
    return [v for v in vals if len(v) >= 4]


def scrub(results: Iterable[Result], secrets: list[str]) -> list[Result]:
    out = []
    for r in results:
        d = r.as_dict()
        for k in ("title", "detail", "hint"):
            for sv in secrets:
                d[k] = d[k].replace(sv, "***")
        out.append(Result(**d))
    return out


def summary(results: list[Result]) -> dict[str, int]:
    return {lv: sum(1 for r in results if r.level == lv) for lv in (PASS, WARN, FAIL)}


def exit_code(results: list[Result]) -> int:
    return 1 if any(r.level == FAIL for r in results) else 0


def as_json(results: list[Result]) -> str:
    return json.dumps({"summary": summary(results), "exit_code": exit_code(results),
                       "results": [r.as_dict() for r in results]}, ensure_ascii=False, indent=2)


def as_text(results: list[Result]) -> str:
    lines = ["llmwiki 진단 (doctor)", "=" * 60]
    for r in results:
        lines.append(f"{_MARK[r.level]} {r.title}")
        if r.detail:
            lines.append(f"       {r.detail}")
        if r.hint and r.level != PASS:
            lines.append(f"       -> 조치: {r.hint}")
    s = summary(results)
    lines += ["=" * 60, f"요약: PASS {s[PASS]} / WARN {s[WARN]} / FAIL {s[FAIL]}",
              "결과: 실패 항목이 있습니다. 위 '조치'를 따라 수정한 뒤 다시 실행하세요." if s[FAIL] else
              "결과: 치명적 문제 없음." + (" (WARN 항목은 확인하세요)" if s[WARN] else "")]
    return "\n".join(lines)
