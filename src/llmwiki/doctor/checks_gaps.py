"""Doctor checks for the OCR command and the per-space PII policy."""
from __future__ import annotations

import os
from collections.abc import Mapping

from ..config import Settings
from ..pipeline import inbox, notes
from ..pipeline.aliases import AliasError, load_aliases
from ..pipeline.ocr_command import OcrError, ocr_from_env
from ..pipeline.pii_policy import PiiPolicyError, load_pii_policy
from .model import Result, fail, ok, warn


def check_ocr_command(environ: Mapping[str, str], probe: bool = False) -> list[Result]:
    if not (environ.get("WIKI_OCR_COMMAND") or "").strip():
        return [warn("ocr.command", "OCR 명령 미구성 (WIKI_OCR_COMMAND 없음)", "스캔 PDF 는 텍스트를 추출하지 못합니다.",
                     "ONSITE-HANDOFF b-5 참고. 외부 OCR 실행 파일 경로를 WIKI_OCR_COMMAND 로 지정하세요.")]
    try:
        engine = ocr_from_env(environ)
    except ValueError as e:
        return [fail("ocr.command", "OCR 설정 오류", str(e), "WIKI_OCR_COMMAND/ARGS/INPUT/TIMEOUT/MAX_OUTPUT 값을 확인하세요.")]
    out = [ok("ocr.command", "OCR 명령 구성됨", f"입력={engine.input_kind} timeout={engine.timeout:g}s (경로/값은 표시하지 않음)")]
    if not probe:
        out.append(warn("ocr.probe", "OCR 실제 호출은 하지 않음", "", "--probe-ocr 로 1x1 PNG/빈 PDF 자가 테스트를 실행하세요."))
        return out
    try:
        text = engine.probe()
        out.append(ok("ocr.probe", "OCR 자가 테스트 통과", f"출력 {len(text)}자 (내용 미표시)"))
    except OcrError as e:
        out.append(fail("ocr.probe", "OCR 자가 테스트 실패", str(e), "외부 명령을 직접 실행해 stdin/stdout 계약(ocr_command.py 상단)을 확인하세요."))
    return out


def check_pii_policy(environ: Mapping[str, str]) -> list[Result]:
    if not (environ.get("WIKI_PII_POLICY_FILE") or "").strip():
        return [ok("pii.policy", "space별 PII 정책 파일 없음", "전역 WIKI_MASK_PII 값만 적용")]
    try:
        policy = load_pii_policy(environ)
    except PiiPolicyError as e:
        return [fail("pii.policy", "PII 정책 파일 오류 (파이프라인 시작 거부됨)", str(e),
                     'JSON 객체 {"dept-a": true, "dept-b/part-1": false} 형식인지, space 이름이 유효한지 확인하세요.')]
    on = sorted(k for k, v in policy.items() if v)
    return [ok("pii.policy", f"space별 PII 정책 {len(policy)}건", f"마스킹 켬: {', '.join(on) or '-'}")]


def check_inbox_aliases(environ: Mapping[str, str], settings: Settings) -> list[Result]:
    if not (environ.get("WIKI_INBOX_ALIASES") or "").strip():
        return [ok("inbox.aliases", "폴더 별칭 파일 없음", "폴더 이름 = space 이름 (예: inbox/dept-a)")]
    try:
        aliases = load_aliases(environ)
    except AliasError as e:
        return [fail("inbox.aliases", "폴더 별칭 파일 오류 (파이프라인 시작 거부됨)", str(e),
                     'JSON 객체 {"인사팀": "dept-hr"} 형식, 키는 Windows 폴더 이름 규칙, 값은 올바른 space 이름이어야 합니다.')]
    root = inbox.inbox_dir(settings)
    missing = sorted(k for k in aliases.entries if not root.joinpath(*k.split("/")).is_dir())
    if missing:
        return [warn("inbox.aliases", f"별칭 {len(aliases.entries)}건 중 {len(missing)}건은 inbox 에 폴더가 없음",
                     ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else ""),
                     "공유 폴더에 해당 폴더를 만들고 AD 그룹 권한을 부여하세요 (OBSIDIAN-MANUAL 3-5).")]
    return [ok("inbox.aliases", f"폴더 별칭 {len(aliases.entries)}건, 모두 폴더 있음")]


def check_inbox_layout(settings: Settings, environ: Mapping[str, str] | None = None) -> list[Result]:
    root = inbox.inbox_dir(settings)
    if not root.is_dir():
        return [warn("inbox.layout", "inbox 폴더가 아직 없음", "", "파이프라인/웹 첫 실행 때 만들어지거나 직접 만드세요.")]
    probe = root / f".doctor-probe-{os.getpid()}.tmp"  # dot-prefixed: the scanner ignores it
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        return [fail("inbox.layout", "inbox 폴더에 쓸 수 없음", type(e).__name__, "서비스 계정에 inbox 전체 Modify 권한이 필요합니다.")]
    try:
        aliases = load_aliases(environ or {})
    except AliasError:
        aliases = None  # reported by inbox.aliases
    stray = linked = 0
    for item in inbox.scan(settings, aliases):
        if item.space is None and item.path.parent == root:
            stray += 1
        elif item.space is None and item.category == notes.LINKED:
            linked += 1
    if stray or linked:
        return [warn("inbox.layout", f"inbox 바로 아래 파일 {stray}개(처리 시 _rejected 로 이동), 연결 폴더/바로가기 {linked}개",
                     "파일 이름은 표시하지 않음", "관리자가 올바른 부서 폴더로 옮기세요. 연결 폴더는 처리되지 않습니다.")]
    return [ok("inbox.layout", "inbox 폴더 쓰기 가능, 루트에 방치된 파일 없음")]
