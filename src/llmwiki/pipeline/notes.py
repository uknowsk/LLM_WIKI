"""Result notes for people who drop files into a shared folder: `<file name>.처리결과.txt` next to where it was.

A note only ever contains a reason CATEGORY, the time and what to do. Never file content, never raw exception text
(parser errors can echo document fragments). Writing a note must never fail the pipeline.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

log = logging.getLogger("llmwiki.pipeline")

UNSUPPORTED = "지원하지 않는 형식"
CORRUPT = "파일이 손상됨"
UNREADABLE = "읽기 실패"
FAILED = "처리 실패"
LOCATION = "폴더 위치가 올바르지 않음"
TOO_LARGE = "파일 크기 초과"
BAD_NAME = "파일 이름 문제"
LINKED = "바로가기/연결 폴더 사용 불가"
NO_ACCESS = "파일을 읽을 권한이 없거나 다른 프로그램이 계속 사용 중입니다"

_ACTION = {
    UNSUPPORTED: "docx / xlsx / pdf / eml / md / txt 중 하나로 저장해서 다시 넣어 주세요.",
    CORRUPT: "파일을 다시 저장(암호 해제 포함)한 뒤 같은 폴더에 다시 넣어 주세요.",
    UNREADABLE: "다른 프로그램에서 열려 있지 않은지 확인하고 다시 넣어 주세요. 스캔 PDF 는 관리자에게 문의하세요.",
    FAILED: "같은 이름으로 한 번 더 넣어 보고, 계속 안 되면 관리자에게 문의하세요.",
    LOCATION: "관리자가 알려 준 부서 폴더에 넣어 주세요.",
    TOO_LARGE: "파일을 나누거나 줄여서 다시 넣어 주세요.",
    BAD_NAME: "파일 이름에서 특수문자(\\ / : * ? \" < > |)와 끝의 점/공백을 없애고 다시 넣어 주세요.",
    NO_ACCESS: "다른 프로그램에서 파일을 닫고 다시 넣어 주세요. 계속 안 되면 관리자에게 문의하세요(폴더 권한 확인 필요).",
    LINKED: "바로가기·연결 폴더는 처리하지 않습니다. 실제 파일을 직접 넣어 주세요.",
}
MAX_NOTES = 9  # base name plus -2 .. -9
_NOTE_RE = re.compile(r"\.처리결과(-\d+)?\.txt$")


def is_note_name(name: str) -> bool:
    """True for notes written by this module (scans must ignore them). NFC-normalised: clients may send NFD."""
    return _NOTE_RE.search(unicodedata.normalize("NFC", name)) is not None


def categorize_error(error: str) -> str:
    """Map a ProcessResult.error string to a category (the text itself is never copied into a note)."""
    for cat in _ACTION:  # already sanitised ("ValueError (파일이 손상됨)")
        if cat in (error or ""):
            return cat
    low = (error or "").lower()
    if "unsupported" in low:
        return UNSUPPORTED
    if "too large" in low:
        return TOO_LARGE
    if any(k in low for k in ("not a valid", "malformed", "invalid xml", "badzip", "corrupt", "zip bomb", "cannot read package")):
        return CORRUPT
    if "permissionerror" in low:
        return NO_ACCESS
    if any(k in low for k in ("oserror", "filenotfound", "no extractable", "ioerror")):
        return UNREADABLE
    return FAILED


def _variants(file_name: str) -> list[str]:
    return [f"{file_name}.처리결과.txt", *(f"{file_name}.처리결과-{n}.txt" for n in range(2, MAX_NOTES + 1))]


def write_note(folder: Path, file_name: str, category: str, now: datetime | None = None) -> Path | None:
    """Create the note (exclusive create; an existing note is never overwritten: next free -N name, max 9)."""
    action = _ACTION.get(category, _ACTION[FAILED])
    file_name = file_name[:200]  # note name must fit NTFS's 255 characters
    body = (
        "이 파일은 위키에 반영되지 않았습니다.\n"
        f"사유: {category}\n"
        f"시각: {(now or datetime.now()).strftime('%Y-%m-%d %H:%M')}\n"
        f"조치: {action}\n"
    )
    try:
        for name in _variants(file_name):
            try:
                with open(folder / name, "x", encoding="utf-8", newline="\n") as f:
                    f.write(body)
                return folder / name
            except FileExistsError:
                continue
    except OSError as exc:  # no permission, name too long, share gone ... never fatal
        log.warning("result note not written (%s)", type(exc).__name__)
    return None


def clear_notes(folder: Path, file_name: str) -> int:
    """Remove stale notes of this exact file name after a later success. Returns how many were removed."""
    removed = 0
    for name in _variants(file_name):
        try:
            (folder / name).unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("stale result note not removed (%s)", type(exc).__name__)
    return removed
