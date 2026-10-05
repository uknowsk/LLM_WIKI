"""python -m llmwiki.doctor [--json] [--probe-context] [--skip-llm] [--skip-embed] [--probe-ocr]"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping

from ..config import load_settings
from . import report
from .checks_gaps import check_inbox_aliases, check_inbox_layout, check_ocr_command, check_pii_policy
from .checks_llm import build_llm, check_chat, check_embed, check_stream
from .checks_self import check_acl, check_ingest
from .checks_sys import check_auth, check_data, check_env, check_ocr, check_python, check_web
from .model import Result, warn


def run_checks(environ: Mapping[str, str], probe_context: bool = False, skip_llm: bool = False,
               skip_embed: bool = False, probe_sizes=None, probe_ocr: bool = False) -> list[Result]:
    results: list[Result] = []
    results += check_python()
    try:
        settings = load_settings(dict(environ))
    except Exception as e:  # noqa: BLE001
        from .model import fail

        return results + [fail("env.settings", "설정을 읽지 못함", f"{type(e).__name__}", "WIKI_* 환경변수 값을 확인하세요.")]
    results += check_env(settings, environ)
    results += check_data(settings)
    results += check_web(settings, environ)
    results += check_auth(settings)
    results += check_ocr(environ)
    results += check_ocr_command(environ, probe_ocr)
    results += check_pii_policy(environ)
    results += check_inbox_aliases(environ, settings)
    results += check_inbox_layout(settings, environ)
    client, res = build_llm(settings, environ)
    results += res
    if client is not None:
        if skip_llm:
            results.append(warn("llm.call", "LLM 호출 검사를 건너뜀 (--skip-llm)"))
        else:
            kw = {"sizes": probe_sizes} if probe_sizes else {}
            results += check_chat(client, probe_context, **kw)
            results += check_stream(settings, environ, client)
    if skip_embed:
        results.append(warn("embed.call", "임베딩 호출 검사를 건너뜀 (--skip-embed)"))
    else:
        results += check_embed(settings, environ)
    results += check_ingest()
    results += check_acl()
    return results


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llmwiki.doctor", description="환경/LLM/임베딩/파서/ACL 자가 진단 (비밀값과 문서 내용은 출력하지 않음)")
    ap.add_argument("--json", action="store_true", help="JSON으로 출력")
    ap.add_argument("--probe-context", action="store_true",
                    help="합성 프롬프트를 점점 키워 컨텍스트 한계를 탐지 (최대 약 25만 자를 LLM으로 전송)")
    ap.add_argument("--skip-llm", action="store_true", help="채팅 LLM 호출 검사를 건너뜀")
    ap.add_argument("--skip-embed", action="store_true", help="임베딩 호출 검사를 건너뜀")
    ap.add_argument("--probe-ocr", action="store_true", help="WIKI_OCR_COMMAND 를 1x1 PNG(또는 빈 PDF)로 실제 실행해 자가 테스트")
    args = ap.parse_args(argv)
    env = os.environ if environ is None else environ
    results = run_checks(env, args.probe_context, args.skip_llm, args.skip_embed, probe_ocr=args.probe_ocr)
    results = report.scrub(results, report.secret_values(env))
    print(report.as_json(results) if args.json else report.as_text(results))
    return report.exit_code(results)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]  (cp949/cp1252 consoles must not crash)
    except (AttributeError, ValueError):
        pass
    raise SystemExit(main())
