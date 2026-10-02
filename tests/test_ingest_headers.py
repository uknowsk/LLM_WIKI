"""Header-row selection: title / metadata / approver rows above the real header never become headers."""
import io
import zipfile
from xml.sax.saxutils import escape

from llmwiki.ingest.docx import parse_docx
from llmwiki.ingest.xlsx import parse_xlsx

SS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
RNS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
REL_T = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
WNS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def _sheet(rows: list[list], merges: list[str] | None = None) -> str:
    """rows of str | int | None -> worksheet XML (None = empty cell, int = number)."""
    out = []
    for r, row in enumerate(rows, 1):
        cells = ""
        for c, v in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if v is None:
                continue
            cells += (f'<c r="{ref}"><v>{v}</v></c>' if isinstance(v, int)
                      else f'<c r="{ref}" t="inlineStr"><is><t>{escape(v)}</t></is></c>')
        out.append(f'<row r="{r}">{cells}</row>')
    mc = f'<mergeCells>{"".join(f"<mergeCell ref=\"{m}\"/>" for m in merges)}</mergeCells>' if merges else ""
    return f"<worksheet {SS}><sheetData>{''.join(out)}</sheetData>{mc}</worksheet>"


def _xlsx(rows: list[list], merges: list[str] | None = None) -> str:
    wb = f'<workbook {SS} {RNS}><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{REL_T}worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    data = _zip({"xl/workbook.xml": wb, "xl/_rels/workbook.xml.rels": rels, "xl/worksheets/sheet1.xml": _sheet(rows, merges)})
    return parse_xlsx(data, "t.xlsx").text.split("## S\n\n", 1)[1]


INCIDENT = [
    ["2026년 3분기 장애 현황"],
    ["작성일", "2026-10-01", None, "작성자", "김서연", None, None],
    ["구분", "장애ID", "발생일", "시스템", "증상", "복구시간(분)", "담당자"],
    ["네트워크", "INC-1", "2026-07-07", "CSW-01", "스위치 다운", 33, "정유나"],
    ["네트워크", "INC-2", "2026-08-05", "AP-3F", "무선 끊김", 41, "서지안"],
    ["합계", "합계", "합계", "합계", "합계", 74, None],
]


def test_incident_status_shape_metadata_row_is_not_header():
    t = _xlsx(INCIDENT, ["A1:G1", "A4:A5"])
    assert t == (
        "2026년 3분기 장애 현황\n작성일: 2026-10-01 · 작성자: 김서연\n\n"
        "| 구분 | 장애ID | 발생일 | 시스템 | 증상 | 복구시간(분) | 담당자 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 네트워크 | INC-1 | 2026-07-07 | CSW-01 | 스위치 다운 | 33 | 정유나 |\n"
        "| 네트워크 | INC-2 | 2026-08-05 | AP-3F | 무선 끊김 | 41 | 서지안 |\n"
        "| 합계 | 합계 | 합계 | 합계 | 합계 | 74 |  |\n\n"
        "### 항목별 값\n"
        "- 네트워크 / INC-1 / 2026-07-07 | 시스템: CSW-01; 증상: 스위치 다운; 복구시간(분): 33; 담당자: 정유나\n"
        "- 네트워크 / INC-2 / 2026-08-05 | 시스템: AP-3F; 증상: 무선 끊김; 복구시간(분): 41; 담당자: 서지안\n"
        "- 합계 | 복구시간(분): 74")


def test_no_flat_section_without_trustworthy_structure():
    assert "### 항목별 값" not in _xlsx(INCIDENT, ["A1:G1"])  # no vertical merge, single-row header


def test_approver_block_above_header_kept_as_lines():
    rows = [["문서", "점검표", None, None, None, "결재", "담당", "팀장"],
            ["점검일", "2026-10-02", None, None, None, "결재", "김", "이"],
            [None] * 8,
            ["항목", "결과", "판정", "비고", "조치", "담당", "확인", "서명"],
            ["온도", "22도", "정상", "-", "-", "김", "O", "이"], ["습도", "47%", "정상", "-", "-", "김", "O", "이"]]
    t = _xlsx(rows)
    assert t.startswith("문서: 점검표 · 결재 / 담당 / 팀장\n점검일: 2026-10-02 · 결재 / 김 / 이\n\n| 항목 | 결과 | 판정 | 비고 |")
    assert "| 온도 | 22도 | 정상 |" in t and "| C |" not in t


def test_headerless_numeric_table_gets_generated_headers():
    t = _xlsx([[1, 2, 3], [4, 5, 6], [7, 8, 9]])
    assert t == "| 열 1 | 열 2 | 열 3 |\n| --- | --- | --- |\n| 1 | 2 | 3 |\n| 4 | 5 | 6 |\n| 7 | 8 | 9 |"


def test_genuine_header_with_empty_cell_uses_position_label():
    t = _xlsx([["이름", "부서", "비고", "연락"], ["가", "A팀", "x", "y"], ["나", "B팀", "z", "w"]])
    assert t.splitlines()[0] == "| 이름 | 부서 | 비고 | 연락 |"
    t = _xlsx([["이름", "부서", None, "연락"], ["가", "A팀", "x", "y"], ["나", "B팀", "z", "w"]])
    assert t.splitlines()[0] == "| 이름 | 부서 | 열 3 | 연락 |"


def test_multilevel_merged_header_still_path():
    rows = [["구분", "상반기", None], [None, "1월", "2월"], ["재무", 10, 20], ["재무2", 30, 40]]
    t = _xlsx(rows, ["A1:A2", "B1:C1"])
    assert t.splitlines()[0] == "| 구분 | 상반기 > 1월 | 상반기 > 2월 |"
    assert "- 재무 | 상반기 > 1월: 10; 상반기 > 2월: 20" in t


def _tc(t, span=None):
    pr = f'<w:tcPr><w:gridSpan w:val="{span}"/></w:tcPr>' if span else ""
    return f"<w:tc>{pr}<w:p><w:r><w:t>{t}</w:t></w:r></w:p></w:tc>"


def _tr(*cells):
    return "<w:tr>" + "".join(cells) + "</w:tr>"


def _docx(body: str) -> str:
    return parse_docx(_zip({"word/document.xml": f"<w:document {WNS}><w:body>{body}</w:body></w:document>"}), "t.docx").text


def test_docx_table_with_title_and_metadata_rows():
    body = ("<w:tbl>" + _tr(_tc("장애 목록", span=3)) + _tr(_tc("작성일"), _tc("2026-10-01"), _tc(""))
            + _tr(_tc("ID"), _tc("증상"), _tc("시간")) + _tr(_tc("A"), _tc("다운"), _tc("5")) + _tr(_tc("B"), _tc("지연"), _tc("7"))
            + "</w:tbl>")
    assert _docx(body) == ("장애 목록\n작성일: 2026-10-01\n\n| ID | 증상 | 시간 |\n| --- | --- | --- |\n"
                           "| A | 다운 | 5 |\n| B | 지연 | 7 |")


def test_docx_key_value_form_has_no_fake_header():
    body = ("<w:tbl>" + _tr(_tc("요청자"), _tc("강민서"), _tc("요청일"), _tc("2026-09-29"))
            + _tr(_tc("대상"), _tc("DB"), _tc("긴급도"), _tc("긴급"))
            + _tr(_tc("사유"), _tc("실수로 삭제", span=3)) + "</w:tbl>")
    t = _docx(body)
    assert t.splitlines()[0] == "| 열 1 | 열 2 | 열 3 | 열 4 |" and "| 요청자 | 강민서 | 요청일 | 2026-09-29 |" in t
