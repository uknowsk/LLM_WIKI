"""`--check`: Korean PASS/WARN/FAIL list built from the doctor's engine checks plus personal-mode checks."""
from __future__ import annotations

import os
import socket
import tempfile
from collections.abc import Mapping
from pathlib import Path

from ..doctor import report
from ..doctor.checks_llm import build_llm, check_chat, check_embed
from ..doctor.checks_self import check_acl, check_ingest
from ..doctor.checks_sys import check_python
from ..doctor.model import Result, fail, ok, warn
from .settings import (PersonalConfigError, personal_environ, personal_home, personal_settings, validate_bind_host,
                       watch_folders)


def _home_check(home: Path) -> Result:
    probe = home
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        with tempfile.NamedTemporaryFile(dir=probe, prefix=".check-", delete=True) as f:
            f.write(b"x")
    except OSError as exc:
        return fail("personal.home", "개인 데이터 폴더에 쓸 수 없음", f"{home} ({type(exc).__name__})",
                    "WIKI_PERSONAL_HOME 을 쓰기 가능한 폴더로 지정하세요.")
    note = "" if home.is_dir() else " (아직 없음: 첫 실행 때 만들어집니다)"
    return ok("personal.home", "개인 데이터 폴더 쓰기 가능", f"{home}{note}")


def _under(path: Path, root: str) -> bool:
    p, r = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


def _acl_check(home: Path, environ: Mapping[str, str]) -> Result | None:
    """A data folder outside the user profile (e.g. on a second drive) usually inherits broad ACLs from the drive root."""
    roots = [environ.get(k) for k in ("LOCALAPPDATA", "USERPROFILE") if environ.get(k)] or [str(Path.home())]
    if any(_under(home, r) for r in roots):
        return None
    return warn("personal.acl", "데이터 폴더가 사용자 프로필 밖에 있음 (다른 사용자가 읽을 수 있는 권한을 상속했을 수 있음)", str(home),
                '본인만 접근하도록 설정하세요 [미검증, 실행하지 않음]: icacls "<폴더>" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"')


def _bind_check(host: str) -> Result:
    try:
        validate_bind_host(host)
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(fam, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
    except PersonalConfigError as exc:
        return fail("personal.bind", "바인드 주소가 루프백이 아님", str(exc), "127.0.0.1 또는 ::1 만 허용됩니다.")
    except OSError as exc:
        return fail("personal.bind", f"{host} 에 바인드할 수 없음", type(exc).__name__,
                    "네트워크 설정(IPv6 비활성 등)을 확인하세요.")
    return ok("personal.bind", f"루프백 바인드 가능 ({host})", "WIKI_HOST 는 개인 모드에서 무시됩니다.")


def _watch_checks(environ: Mapping[str, str], home: Path) -> list[Result]:
    try:
        roots = watch_folders(environ, home)
    except PersonalConfigError as exc:
        return [fail("personal.watch", "감시 폴더 설정 오류", str(exc),
                     "WIKI_PERSONAL_WATCH 는 절대 경로 목록(; 구분 또는 JSON 배열)입니다.")]
    if not roots:
        return [ok("personal.watch", "감시 폴더 없음 (inbox 폴더만 사용)")]
    out = []
    home_real = os.path.normcase(os.path.realpath(home))
    for r in roots:
        real = os.path.normcase(os.path.realpath(r))
        if real == home_real or real.startswith(home_real + os.sep):
            out.append(warn("personal.watch", "감시 폴더가 데이터 폴더 안에 있어 무시됨", str(r), "다른 폴더를 지정하세요."))
        elif not r.is_dir():
            out.append(warn("personal.watch", "감시 폴더가 없거나 폴더가 아님", str(r), "경로 철자와 드라이브 연결을 확인하세요."))
        else:
            try:
                with os.scandir(r) as it:
                    n = sum(1 for _ in it)
                out.append(ok("personal.watch", "감시 폴더 읽기 가능", f"{r} (항목 {n}개)"))
            except OSError as exc:
                out.append(fail("personal.watch", "감시 폴더를 읽을 수 없음", f"{r} ({type(exc).__name__})",
                                "폴더 권한을 확인하세요."))
    return out


def run_check(environ: Mapping[str, str], host: str = "127.0.0.1", probe_context: bool = False) -> tuple[list[Result], int]:
    env = personal_environ(environ)
    home = personal_home(env)
    results = [*check_python(), _bind_check(host), _home_check(home), *_watch_checks(env, home)]
    acl = _acl_check(home, env)
    if acl is not None:
        results.append(acl)
    settings = personal_settings(env, home)
    client, res = build_llm(settings, env)  # includes the WIKI_LLM_CONTEXT_TOKENS (context size) check
    results += res
    if client is not None:
        results += check_chat(client, probe_context)
    results += check_embed(settings, env)
    results += check_ingest()
    results += check_acl()
    results = report.scrub(results, report.secret_values(env))
    return results, report.exit_code(results)
