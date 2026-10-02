"""Self-tests on built-in synthetic data: parsers run on this machine, and the ACL invariant holds."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path

from ..acl import can_read, filter_readable
from ..auth import User
from ..config import Settings
from ..ingest.docx import parse_docx
from ..ingest.text import parse_text
from ..ingest.xlsx import parse_xlsx
from .model import Result, fail, ok

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MD = "# 진단용 문서\n\n본문 DOCTOR-MD-MARK 입니다.\n"


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def synthetic_docx() -> bytes:
    def tc(t, span=""):
        return f'<w:tc><w:tcPr>{span}</w:tcPr><w:p><w:r><w:t>{t}</w:t></w:r></w:p></w:tc>'
    span = '<w:gridSpan w:val="2"/>'
    head, left, right = tc("합병머리", span), tc("좌값"), tc("우값")
    body = f"<w:p><w:r><w:t>DOCTOR-DOCX-MARK</w:t></w:r></w:p><w:tbl><w:tr>{head}</w:tr><w:tr>{left}{right}</w:tr></w:tbl>"
    return _zip({"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'})


def synthetic_xlsx() -> bytes:
    def c(ref, t):
        return f'<c r="{ref}" t="inlineStr"><is><t>{t}</t></is></c>'
    sheet = (f'<worksheet xmlns="{_S}"><sheetData><row r="1">{c("A1", "병합머리")}</row>'
             f'<row r="2">{c("A2", "가")}{c("B2", "DOCTOR-XLSX-MARK")}</row></sheetData>'
             '<mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells></worksheet>')
    wb = f'<workbook xmlns="{_S}" xmlns:r="{_R}"><sheets><sheet name="진단" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            f'Type="{_R}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    return _zip({"xl/workbook.xml": wb, "xl/_rels/workbook.xml.rels": rels, "xl/worksheets/sheet1.xml": sheet})


def check_ingest() -> list[Result]:
    cases = [
        ("md", lambda: parse_text(MD.encode("utf-8"), "doctor.md").text, ["DOCTOR-MD-MARK"]),
        ("docx", lambda: parse_docx(synthetic_docx(), "doctor.docx").text, ["DOCTOR-DOCX-MARK", "합병머리", "좌값", "우값"]),
        ("xlsx(병합 셀)", lambda: parse_xlsx(synthetic_xlsx(), "doctor.xlsx").text, ["DOCTOR-XLSX-MARK", "병합머리"]),
    ]
    out: list[Result] = []
    for name, fn, needles in cases:
        try:
            text = fn()
            missing = [n for n in needles if n not in text]
            out.append(ok(f"ingest.{name.split('(')[0]}", f"파서 자가 테스트: {name}", f"{len(text)}자 추출") if not missing else
                       fail(f"ingest.{name.split('(')[0]}", f"파서 자가 테스트 실패: {name}", f"기대 문자열 누락: {missing}",
                            "ingest 파서가 이 환경에서 올바르게 동작하지 않습니다. Python 버전/인코딩(UTF-8) 설정을 확인하세요."))
        except Exception as e:  # noqa: BLE001
            out.append(fail(f"ingest.{name.split('(')[0]}", f"파서 자가 테스트 오류: {name}", type(e).__name__,
                            "Python 표준 라이브러리(zipfile, xml)가 정상인지, 개발 파일이 손상되지 않았는지 확인하세요."))
    return out


def check_acl() -> list[Result]:
    ua = User("doc-a", "A", "dept-a", None, frozenset({"dept-a"}))
    ub = User("doc-b", "B", "dept-b", None, frozenset({"dept-b"}))
    try:
        matrix = (can_read(ua, ["dept-a"]) and can_read(ub, ["dept-b"]) and not can_read(ua, ["dept-b"])
                  and not can_read(ub, ["dept-a"]) and not can_read(ua, ["dept-a", "dept-b"]) and not can_read(ua, [])
                  and filter_readable(ua, ["x", "y"], lambda i: ["dept-a"] if i == "x" else ["dept-b"]) == ["x"])
    except Exception as e:  # noqa: BLE001
        return [fail("acl.matrix", "ACL 자가 테스트 오류", type(e).__name__)]
    if not matrix:
        return [fail("acl.matrix", "ACL 규칙 위반 감지 (다른 공간의 문서가 읽힘)", "", "코드가 변경/손상되었을 수 있습니다. 운영을 중단하고 원본과 비교하세요.")]
    out = [ok("acl.matrix", "ACL 규칙 (두 사용자, 공간 간 접근 거부)", "A는 dept-a만, B는 dept-b만, 혼합 출처는 모두 보유해야 열람")]
    try:
        out.append(_end_to_end(ua, ub))
    except Exception as e:  # noqa: BLE001
        out.append(fail("acl.e2e", "ACL 종단 자가 테스트 오류", type(e).__name__, "compile/query 엔진이 이 환경에서 동작하는지 확인하세요."))
    return out


def _end_to_end(ua: User, ub: User) -> Result:
    from ..audit import AuditLog
    from ..engine.compile import Compiler
    from ..engine.llm import FakeLLM
    from ..engine.query import NO_EVIDENCE, QueryService
    from ..engine.store import Store
    from ..models import RawRecord

    mark = "DOCTOR-ACL-SECRET-MARK"
    with tempfile.TemporaryDirectory(prefix="llmwiki-doctor-", ignore_cleanup_errors=True) as t:
        root = Path(t)
        settings = Settings("development", root, "dev", "http://127.0.0.1:1/v1", "m", False)
        store, audit = Store(settings.db_path), AuditLog(settings.db_path)
        triage = json.dumps({"decision": "New", "target": None, "topic": "general", "title": "진단 문서",
                             "body": f"휴가 신청 절차 {mark}", "related": []}, ensure_ascii=False)
        llm = FakeLLM(lambda s, p: "답변 [1]" if "CONTEXT:" in p else triage)
        text = f"휴가 신청 절차 {mark}"
        rec = RawRecord("raw/dept-a/doctor.md", "dept-a", hashlib.sha256(text.encode()).hexdigest())
        f = root / rec.raw_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(text.encode("utf-8"))
        Compiler(settings, store, llm).compile(rec, text)
        qs = QueryService(settings, store, llm, audit)
        calls_before = len(llm.calls)
        deny = qs.query(ub, "휴가 신청 절차")
        leaked = any(mark in p for _, p in llm.calls[calls_before:]) or mark in deny.answer
        allow = qs.query(ua, "휴가 신청 절차")
        allowed = any(mark in p for _, p in llm.calls[calls_before:]) and bool(allow.citations)
        store.close()
    if leaked or deny.answer != NO_EVIDENCE:
        return fail("acl.e2e", "ACL 위반: 권한 없는 사용자의 질의에 다른 공간 문서가 사용됨", "", "즉시 운영을 중단하고 코드를 원본과 비교하세요.")
    if not allowed:
        return fail("acl.e2e", "정당한 사용자가 자기 공간 문서를 찾지 못함", "", "compile/검색 엔진 동작을 확인하세요.")
    return ok("acl.e2e", "ACL 종단 자가 테스트", "문서 투입 -> B 질의 거부/LLM 프롬프트에 미포함, A 질의 허용")
