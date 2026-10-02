"""Doctor checks for the OCR command and the per-space PII policy."""
from __future__ import annotations

from collections.abc import Mapping

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
