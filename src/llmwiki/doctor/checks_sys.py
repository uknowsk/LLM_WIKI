"""Environment / filesystem / web / auth checks (no network)."""
from __future__ import annotations

import platform
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from ..auth import get_provider
from ..config import Settings
from ..web.webconfig import MIN_SECRET_LEN, is_dev_env, load_web_config
from .model import Result, fail, ok, warn

MIN_FREE_FAIL = 200 * 1024 * 1024
MIN_FREE_WARN = 2 * 1024 * 1024 * 1024


def check_python() -> list[Result]:
    v = sys.version_info
    out = [ok("sys.python", "Python 버전", f"{v.major}.{v.minor}.{v.micro}") if v >= (3, 11) else
           fail("sys.python", "Python 버전이 너무 낮음", f"{v.major}.{v.minor}.{v.micro}",
                "Python 3.11 이상을 설치하고 해당 인터프리터로 다시 실행하세요.")]
    out.append(ok("sys.platform", "플랫폼", f"{platform.system()} {platform.release()} / {platform.machine()} / "
                  f"{platform.python_implementation()}"))
    return out


def check_env(settings: Settings, environ: Mapping[str, str]) -> list[Result]:
    dev = is_dev_env(settings)
    out: list[Result] = []
    if dev:
        out.append(warn("env.mode", f"WIKI_ENV={settings.env} (개발/테스트 모드)",
                        "dev 인증과 임시 세션 비밀키가 허용됩니다.", "운영 서버에서는 WIKI_ENV를 설정하지 않거나 production으로 두세요."))
    else:
        out.append(ok("env.mode", f"WIKI_ENV={settings.env} (운영 모드, 엄격)"))
    secret = (environ.get("WIKI_SESSION_SECRET") or "").strip()
    if not secret:
        out.append(warn("env.secret", "WIKI_SESSION_SECRET 미설정 (개발 모드: 재시작 시 세션 소멸)", "", "운영에서는 필수입니다.")
                   if dev else
                   fail("env.secret", "WIKI_SESSION_SECRET 미설정", "운영 모드에서는 웹 서버가 시작되지 않습니다.",
                        f"{MIN_SECRET_LEN}자 이상의 무작위 문자열을 설정하세요. 예: "
                        "python -c \"import secrets; print(secrets.token_urlsafe(48))\""))
    elif len(secret) < MIN_SECRET_LEN:
        out.append(warn("env.secret", f"WIKI_SESSION_SECRET이 짧음 ({len(secret)}자)", "", f"{MIN_SECRET_LEN}자 이상 필요") if dev else
                   fail("env.secret", f"WIKI_SESSION_SECRET이 짧음 ({len(secret)}자)", "운영 모드 최소 길이 미달",
                        f"{MIN_SECRET_LEN}자 이상으로 다시 생성하세요."))
    else:
        out.append(ok("env.secret", "WIKI_SESSION_SECRET 설정됨", f"길이 {len(secret)}자 (값은 표시하지 않음)"))
    origins = [o for o in (environ.get("WIKI_ALLOWED_ORIGINS") or "").split(",") if o.strip()]
    if origins:
        out.append(ok("env.origins", "WIKI_ALLOWED_ORIGINS 설정됨", f"{len(origins)}개: " + ", ".join(o.strip() for o in origins)))
    elif dev:
        out.append(ok("env.origins", "WIKI_ALLOWED_ORIGINS 미설정 (개발 모드에서는 불필요)"))
    else:
        out.append(fail("env.origins", "WIKI_ALLOWED_ORIGINS 미설정", "운영 모드에서는 웹 서버가 시작되지 않습니다.",
                        "브라우저가 접속하는 정확한 origin을 쉼표로 지정하세요. 예: WIKI_ALLOWED_ORIGINS=https://wiki.corp.example"))
    return out


def _writable(d: Path) -> str | None:
    """None if a file can be created and removed in d, else the error class name."""
    try:
        with tempfile.NamedTemporaryFile(dir=d, prefix=".doctor-", delete=True) as f:
            f.write(b"x")
        return None
    except OSError as e:
        return type(e).__name__


def _existing_ancestor(p: Path) -> Path:
    p = p.resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def check_data(settings: Settings) -> list[Result]:
    d = settings.data_dir
    out: list[Result] = []
    probe = _existing_ancestor(d)
    if d.is_dir():
        err = _writable(d)
        out.append(ok("fs.data", "데이터 폴더 쓰기 가능", str(d.resolve())) if err is None else
                   fail("fs.data", "데이터 폴더에 쓸 수 없음", f"{d} ({err})",
                        "WIKI_DATA_DIR를 쓰기 가능한 경로로 지정하거나 폴더 권한을 확인하세요."))
    else:
        err = _writable(probe)
        out.append(warn("fs.data", "데이터 폴더가 아직 없음 (첫 실행 시 생성됨)", str(d),
                        "") if err is None else
                   fail("fs.data", "데이터 폴더를 만들 수 없음", f"{d} (상위 {probe}: {err})",
                        "WIKI_DATA_DIR를 쓰기 가능한 경로로 지정하세요."))
    try:
        free = shutil.disk_usage(probe).free
        mb = free // (1024 * 1024)
        out.append(fail("fs.disk", "디스크 여유 공간 부족", f"{mb} MB", "공간을 확보하거나 WIKI_DATA_DIR를 다른 드라이브로 옮기세요.")
                   if free < MIN_FREE_FAIL else
                   warn("fs.disk", "디스크 여유 공간이 적음", f"{mb} MB", "문서가 늘면 부족해질 수 있습니다.")
                   if free < MIN_FREE_WARN else ok("fs.disk", "디스크 여유 공간", f"{mb} MB"))
    except OSError as e:
        out.append(warn("fs.disk", "디스크 여유 공간을 확인하지 못함", type(e).__name__))
    out.append(_check_wal(d if d.is_dir() and _writable(d) is None else None))
    for name, p in (("inbox", d / "inbox"), ("raw", settings.raw_dir), ("wiki", settings.wiki_dir)):
        out.append(ok(f"fs.{name}", f"{name}/ 폴더 존재", str(p)) if p.is_dir() else
                   warn(f"fs.{name}", f"{name}/ 폴더가 아직 없음", str(p),
                        "파이프라인/웹을 처음 실행하면 만들어집니다. 사용자 공간별 하위 폴더: inbox/<space>/ 에 파일을 넣으세요."))
    return out


def _check_wal(d: Path | None) -> Result:
    where = "데이터 폴더" if d is not None else "임시 폴더(데이터 폴더를 쓸 수 없어 대신 검사)"
    try:
        with tempfile.TemporaryDirectory(dir=d, prefix=".doctor-db-", ignore_cleanup_errors=True) as t:
            con = sqlite3.connect(str(Path(t) / "t.db"))
            try:
                mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                con.execute("CREATE TABLE t(x)")
                con.execute("INSERT INTO t VALUES (1)")
                con.commit()
            finally:
                con.close()
        if str(mode).lower() == "wal":
            return ok("fs.wal", "SQLite WAL 모드 동작", f"sqlite {sqlite3.sqlite_version}, {where}")
        return warn("fs.wal", f"SQLite WAL 모드를 쓸 수 없음 (journal_mode={mode})", where,
                    "네트워크 드라이브/동기화 폴더(OneDrive 등)일 수 있습니다. WIKI_DATA_DIR를 로컬 디스크로 지정하세요.")
    except (sqlite3.Error, OSError) as e:
        return fail("fs.wal", "SQLite를 사용할 수 없음", f"{type(e).__name__}", "데이터 폴더 위치/권한을 확인하세요.")


def check_web(settings: Settings, environ: Mapping[str, str]) -> list[Result]:
    try:
        cfg = load_web_config(settings, dict(environ))
    except (RuntimeError, ValueError) as e:  # messages name variables only, never values
        return [fail("web.config", "웹 설정 오류", str(e), "위 메시지의 환경변수를 설정/수정하세요.")]
    out = [ok("web.config", "웹 설정 로드", f"host={cfg.host} port={cfg.port} cookie_secure={cfg.cookie_secure} "
              f"idle={cfg.idle_ttl}s absolute={cfg.absolute_ttl}s upload<={cfg.max_upload_bytes // (1024 * 1024)}MB")]
    if cfg.host not in ("127.0.0.1", "localhost", "::1") and not cfg.cookie_secure:
        out.append(warn("web.cookie", "외부 접속 가능한 주소인데 Secure 쿠키가 꺼져 있음", f"host={cfg.host}",
                        "HTTPS 뒤에서 운영하고 WIKI_COOKIE_SECURE=1 로 두세요."))
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        out.append(warn("web.bind", "웹 서버가 모든/외부 인터페이스에 바인딩됨", f"host={cfg.host}",
                        "리버스 프록시(HTTPS) 뒤에 두고 방화벽으로 접근을 제한하세요."))
    return out


def check_auth(settings: Settings) -> list[Result]:
    try:
        get_provider(settings, {})
        if settings.auth_provider == "dev":
            return [warn("auth.provider", "인증: dev (개발 전용)", "", "운영에서는 saml 등 실제 인증 제공자가 필요합니다.")]
        return [ok("auth.provider", f"인증 제공자: {settings.auth_provider}")]
    except NotImplementedError:
        return [warn("auth.provider", "인증: SAML은 이 빌드에 구현되어 있지 않음", f"WIKI_AUTH_PROVIDER={settings.auth_provider}",
                     "현장에서 AuthProvider(llmwiki/auth.py)를 구현해 연결해야 합니다. 그 전에는 웹 로그인이 동작하지 않습니다.")]
    except (RuntimeError, ValueError) as e:
        return [fail("auth.provider", "인증 제공자 설정 오류", str(e),
                     "WIKI_AUTH_PROVIDER 값(dev|saml)과 WIKI_ENV를 확인하세요. dev 인증은 개발/테스트 모드에서만 허용됩니다.")]


def check_ocr(environ: Mapping[str, str]) -> list[Result]:
    names = sorted(k for k in environ if k.startswith("WIKI_OCR"))
    if names:
        return [ok("ocr.slot", "OCR 설정 감지", ", ".join(names) + " (값은 표시하지 않음)")]
    return [warn("ocr.slot", "OCR 미구성", "스캔 PDF/이미지는 텍스트를 추출하지 못합니다.",
                 "OCR 엔진이 정해지면 해당 환경변수(WIKI_OCR_*)를 설정하세요. 텍스트 PDF/Office/eml은 영향 없습니다.")]
