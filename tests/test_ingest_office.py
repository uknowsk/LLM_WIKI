"""xlsx / docx parsers: merged cells, number/date formats, hardening. Fixtures are hand-written OOXML."""
import io
import zipfile
from datetime import date

import pytest

from llmwiki.config import load_settings
from llmwiki.ingest.docx import parse_docx
from llmwiki.ingest.pii import mask_document
from llmwiki.ingest.save import save_raw
from llmwiki.ingest.xlsx import parse_xlsx

SS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
RNS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
REL_T = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def _zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def _sst_cell(ref, idx, s=0):
    return f'<c r="{ref}" t="s" s="{s}"><v>{idx}</v></c>'


STRINGS = ["구분", "항목", "상반기", "1월", "2월", "3월", "4월", "5월", "6월", "재무", "매출", "비용", "이익"]
IDX = {s: i for i, s in enumerate(STRINGS)}


def _xlsx(extra_wb: str = "", sheet1_override: str | None = None, rels_extra: str = "", files_extra=None) -> bytes:
    def row(r, cells):
        return f'<row r="{r}">{cells}</row>'

    months = "CDEFGH"
    r1 = _sst_cell("A1", IDX["구분"]) + _sst_cell("B1", IDX["항목"]) + _sst_cell("C1", IDX["상반기"])
    r2 = "".join(_sst_cell(f"{c}2", IDX[f"{i}월"]) for i, c in enumerate(months, 1))
    r3 = (_sst_cell("A3", IDX["재무"]) + _sst_cell("B3", IDX["매출"])
          + '<c r="C3" s="1"><v>1250000</v></c><c r="D3" s="2"><v>0.125</v></c>'
          + '<c r="E3" s="3"><v>46023</v></c><c r="F3" s="1"><f>SUM(C3:C3)</f><v>1250000</v></c>'
          + '<c r="G3" t="b"><v>1</v></c><c r="H3" t="inlineStr"><is><r><t>인라</t></r><r><t>인</t></r></is></c>')
    r4 = (_sst_cell("B4", IDX["비용"]) + '<c r="C4" s="4"><v>46023.5</v></c><c r="D4" s="5"><v>3500</v></c>'
          + '<c r="E4" t="e"><v>#DIV/0!</v></c><c r="F4"><v>0.1</v></c>')
    r5 = _sst_cell("B5", IDX["이익"]) + '<c r="C5"><v>7</v></c>'
    merges = ["A1:A2", "B1:B2", "C1:H1", "A3:A5"]
    sheet1 = sheet1_override or (
        f"<worksheet {SS}><sheetData>{row(1, r1)}{row(2, r2)}{row(3, r3)}{row(4, r4)}{row(5, r5)}</sheetData>"
        f'<mergeCells count="{len(merges)}">' + "".join(f'<mergeCell ref="{m}"/>' for m in merges) + "</mergeCells></worksheet>")
    sheet2 = (f'<worksheet {SS}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>메모</t></is></c>'
              '<c r="C1" t="inlineStr"><is><t>비고</t></is></c></row>'
              '<row r="3"><c r="A3" t="inlineStr"><is><t>a|b</t></is></c><c r="C3" t="inlineStr"><is><t>x\ny</t></is></c></row>'
              "</sheetData></worksheet>")
    hidden = f'<worksheet {SS}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>비밀</t></is></c></row></sheetData></worksheet>'
    sst = f"<sst {SS}>" + "".join(
        f"<si><r><t>{s[0]}</t></r><r><t>{s[1:]}</t></r></si>" if len(s) > 1 else f"<si><t>{s}</t></si>" for s in STRINGS) + "</sst>"
    styles = (f'<styleSheet {SS}><numFmts count="2"><numFmt numFmtId="164" formatCode="0.0%"/>'
              '<numFmt numFmtId="165" formatCode="&quot;₩&quot;#,##0"/></numFmts>'
              '<cellXfs count="6"><xf numFmtId="0"/><xf numFmtId="3"/><xf numFmtId="164"/><xf numFmtId="14"/>'
              '<xf numFmtId="22"/><xf numFmtId="165"/></cellXfs></styleSheet>')
    wb = (f'<workbook {SS} {RNS}><workbookPr/><sheets><sheet name="실적" sheetId="1" r:id="rId1"/>'
          '<sheet name="메모" sheetId="2" r:id="rId2"/><sheet name="숨김" sheetId="3" state="hidden" r:id="rId3"/>'
          f"{extra_wb}</sheets></workbook>")
    rels = ("<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
            f'<Relationship Id="rId1" Type="{REL_T}worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{REL_T}worksheet" Target="/xl/worksheets/sheet2.xml"/>'
            f'<Relationship Id="rId3" Type="{REL_T}worksheet" Target="worksheets/sheet3.xml"/>'
            f'<Relationship Id="rId4" Type="{REL_T}styles" Target="styles.xml"/>'
            f'<Relationship Id="rId5" Type="{REL_T}sharedStrings" Target="sharedStrings.xml"/>{rels_extra}</Relationships>')
    core = ('<cp:coreProperties xmlns:cp="x" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/"><dc:title>월별 실적</dc:title>'
            "<dcterms:created>2026-01-05T00:00:00Z</dcterms:created>"
            "<dcterms:modified>2026-02-10T09:00:00Z</dcterms:modified></cp:coreProperties>")
    files = {"xl/workbook.xml": wb, "xl/_rels/workbook.xml.rels": rels, "xl/worksheets/sheet1.xml": sheet1,
             "xl/worksheets/sheet2.xml": sheet2, "xl/worksheets/sheet3.xml": hidden, "xl/styles.xml": styles,
             "xl/sharedStrings.xml": sst, "docProps/core.xml": core}
    files.update(files_extra or {})
    return _zip(files)


def _table_rows(text: str, sheet: str) -> list[list[str]]:
    block = text.split(f"## {sheet}\n\n", 1)[1].split("\n\n## ", 1)[0].split("\n\n###", 1)[0]
    return [[c.strip() for c in ln.strip("|").split(" | ")] for ln in block.splitlines() if ln.startswith("|")]


# ---------------- xlsx ----------------
def test_xlsx_merged_header_and_vertical_label():
    d = parse_xlsx(_xlsx(), "실적.xlsx")
    assert d.title == "월별 실적" and d.metadata["date"] == "2026-02-10"
    rows = _table_rows(d.text, "실적")
    assert rows[0] == ["구분", "항목"] + [f"상반기 > {i}월" for i in range(1, 7)]
    assert [r[0] for r in rows[2:]] == ["재무", "재무", "재무"]  # vertical merge repeated on every row
    assert [r[1] for r in rows[2:]] == ["매출", "비용", "이익"]


def test_xlsx_value_formats():
    d = parse_xlsx(_xlsx(), "a.xlsx")
    r3, r4, r5 = _table_rows(d.text, "실적")[2:]
    assert r3[2:] == ["1,250,000", "12.5%", "2026-01-01", "1,250,000", "TRUE", "인라인"]
    assert r4[2:6] == ["2026-01-01 12:00", "₩3,500", "#DIV/0!", "0.1"]
    assert r5[2] == "7"


def test_xlsx_flattened_path_section():
    d = parse_xlsx(_xlsx(), "a.xlsx")
    assert "### 항목별 값" in d.text
    assert "- 재무 / 매출 | 상반기 > 1월: 1,250,000; 상반기 > 2월: 12.5%; 상반기 > 3월: 2026-01-01;" in d.text
    assert "- 재무 / 이익 | 상반기 > 1월: 7" in d.text


def test_xlsx_sheets_hidden_and_escaping():
    d = parse_xlsx(_xlsx(), "a.xlsx")
    assert "## 실적" in d.text and "## 메모" in d.text and "비밀" not in d.text
    assert d.metadata["hidden_sheets"] == "숨김" and d.metadata["sheets"] == "실적, 메모"
    memo = d.text.split("## 메모\n\n")[1]
    assert "| 메모 | 비고 |" in memo
    assert "a\\|b" in memo and "x<br>y" in memo
    assert "비밀" in parse_xlsx(_xlsx(), "a.xlsx", include_hidden=True).text


def test_xlsx_empty_rows_and_columns_skipped():
    d = parse_xlsx(_xlsx(), "a.xlsx")
    memo = d.text.split("## 메모\n\n")[1]
    assert memo.count("\n|") == 2 and "| B |" not in memo  # header + sep + 1 data row; empty col B/row 2 dropped


def test_xlsx_1904_date_system():
    sheet = f'<worksheet {SS}><sheetData><row r="1"><c r="A1" s="3"><v>0</v></c></row></sheetData></worksheet>'
    wb = (f'<workbook {SS} {RNS}><workbookPr date1904="1"/><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{REL_T}worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    styles = f'<styleSheet {SS}><cellXfs><xf numFmtId="0"/><xf numFmtId="0"/><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>'
    d = parse_xlsx(_zip({"xl/workbook.xml": wb, "xl/_rels/workbook.xml.rels": rels,
                         "xl/worksheets/sheet1.xml": sheet, "xl/styles.xml": styles}), "d.xlsx")
    assert "1904-01-01" in d.text and d.title == "d"


def test_xlsx_cell_cap_truncates():
    cells = "".join(f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>v{i}</t></is></c></row>' for i in range(1, 20103))
    d = parse_xlsx(_xlsx(sheet1_override=f"<worksheet {SS}><sheetData>{cells}</sheetData></worksheet>"), "big.xlsx")
    assert d.metadata["truncated"] == "실적" and "20000셀 제한" in d.text and "v20000" in d.text and "v20001" not in d.text


def test_xlsx_huge_merge_range_is_clipped():
    sheet = (f'<worksheet {SS}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>T</t></is></c></row>'
             '<row r="2"><c r="B2" t="inlineStr"><is><t>x</t></is></c></row></sheetData>'
             '<mergeCells><mergeCell ref="A1:XFD1048576"/></mergeCells></worksheet>')
    d = parse_xlsx(_xlsx(sheet1_override=sheet), "m.xlsx")
    assert "## 실적" in d.text


def test_xlsx_path_traversal_sheet_target_harmless():
    wb_extra = '<sheet name="../../etc/passwd" sheetId="9" r:id="rId9"/>'
    rel = f'<Relationship Id="rId9" Type="{REL_T}worksheet" Target="../../../etc/passwd"/>'
    d = parse_xlsx(_xlsx(extra_wb=wb_extra, rels_extra=rel), "t.xlsx")
    assert "root:" not in d.text and "## 실적" in d.text
    assert "../../etc/passwd" in d.metadata["sheets"]


def test_xlsx_invalid_inputs():
    with pytest.raises(ValueError):
        parse_xlsx(b"not a zip", "bad.xlsx")
    with pytest.raises(ValueError):
        parse_xlsx(_zip({"x.txt": "hi"}), "nowb.xlsx")
    with pytest.raises(ValueError):
        parse_xlsx(_xlsx(), "big.xlsx", max_bytes=100)
    with pytest.raises(ValueError):
        parse_xlsx(_xlsx(files_extra={"xl/worksheets/sheet1.xml": "<worksheet"}), "broken.xlsx")


def test_xlsx_zip_bomb_rejected():
    bomb = _zip({"xl/workbook.xml": "<a>" + "A" * 5_000_000 + "</a>"})
    assert len(bomb) < 100_000
    with pytest.raises(ValueError):
        parse_xlsx(bomb, "bomb.xlsx", max_bytes=1_000_000)


LAUGHS = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
          '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>')


def test_xlsx_entity_bomb_and_xxe_rejected():
    bomb = LAUGHS + f'<worksheet {SS}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&lol3;</t></is></c></row></sheetData></worksheet>'
    with pytest.raises(ValueError):
        parse_xlsx(_xlsx(files_extra={"xl/worksheets/sheet1.xml": bomb}), "lol.xlsx")
    xxe = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><sst ' + SS + "><si><t>&e;</t></si></sst>"
    with pytest.raises(ValueError):
        parse_xlsx(_xlsx(files_extra={"xl/sharedStrings.xml": xxe}), "xxe.xlsx")


def test_xlsx_pii_masked_via_mask_document_and_save(tmp_path):
    sheet = (f'<worksheet {SS}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>이름</t></is></c>'
             '<c r="B1" t="inlineStr"><is><t>연락처</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>홍길동</t></is></c>'
             '<c r="B2" t="inlineStr"><is><t>010-1234-5678</t></is></c></row></sheetData></worksheet>')
    d = parse_xlsx(_xlsx(sheet1_override=sheet), "010-1234-5678.xlsx")
    assert "5678" in d.text
    masked, counts = mask_document(d)
    assert "5678" not in masked.text and "[PHONE]" in masked.text and counts["phone"] >= 1
    s = load_settings({"WIKI_DATA_DIR": str(tmp_path), "WIKI_MASK_PII": "1"})
    r = save_raw(d, "d", s, today=date(2026, 10, 2))
    assert "5678" not in (tmp_path / r.raw_path).read_text(encoding="utf-8")


# ---------------- docx ----------------
WNS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"')


def _p(t):
    return f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"


def _tc(t, span=None, vm=None):
    pr = ""
    if span:
        pr += f'<w:gridSpan w:val="{span}"/>'
    if vm is not None:
        pr += '<w:vMerge w:val="restart"/>' if vm == "restart" else "<w:vMerge/>"
    return f"<w:tc><w:tcPr>{pr}</w:tcPr>{_p(t) if t is not None else '<w:p/>'}</w:tc>"


def _tr(*cells):
    return "<w:tr>" + "".join(cells) + "</w:tr>"


def _docx(body: str, header: str | None = None, extra=None) -> bytes:
    files = {"word/document.xml": f"<w:document {WNS}><w:body>{body}</w:body></w:document>"}
    if header:
        files["word/header1.xml"] = f"<w:hdr {WNS}>{header}</w:hdr>"
    files.update(extra or {})
    return _zip(files)


FORM = (
    _p("지출 결의서")
    + "<w:tbl>"
    + _tr(_tc("구분", vm="restart"), _tc("상반기", span=2))
    + _tr(_tc(None, vm="cont"), _tc("1분기"), _tc("2분기"))
    + _tr(_tc("재무", vm="restart"), _tc("100"), _tc("200"))
    + _tr(_tc(None, vm="cont"), _tc("300"), _tc("400"))
    + "</w:tbl>"
    + _p("결재")
    + "<w:tbl>" + _tr(_tc("담당"), _tc("팀장"), _tc("부서장")) + _tr(_tc("홍길동"), _tc("김팀장"), _tc("이부장")) + "</w:tbl>"
)


def test_docx_gridspan_vmerge_expanded_in_order():
    d = parse_docx(_docx(FORM), "f.docx")
    t = d.text
    assert t.index("지출 결의서") < t.index("| 구분 |") < t.index("결재") < t.index("| 담당 |")
    assert "| 구분 | 상반기 > 1분기 | 상반기 > 2분기 |" in t
    assert "| 재무 | 100 | 200 |\n| 재무 | 300 | 400 |" in t  # vertically merged label repeated
    assert "### 항목별 값\n- 재무 | 상반기 > 1분기: 100; 상반기 > 2분기: 200\n- 재무 | 상반기 > 1분기: 300; 상반기 > 2분기: 400" in t
    assert "| 담당 | 팀장 | 부서장 |\n| --- | --- | --- |\n| 홍길동 | 김팀장 | 이부장 |" in t


def test_docx_horizontal_header_repeated_over_each_spanned_col():
    body = ("<w:tbl>" + _tr(_tc("항목", vm="restart"), _tc("상반기", span=3)) + _tr(_tc(None, vm="cont"), _tc("a"), _tc("b"), _tc("c"))
            + _tr(_tc("x"), _tc("1"), _tc("2"), _tc("3")) + "</w:tbl>")
    t = parse_docx(_docx(body), "h.docx").text
    assert "| 항목 | 상반기 > a | 상반기 > b | 상반기 > c |" in t


def test_docx_nested_table_follows_parent():
    inner = "<w:tbl>" + _tr(_tc("내부1"), _tc("내부2")) + _tr(_tc("가"), _tc("나")) + "</w:tbl>"
    outer = ("<w:tbl>" + _tr(_tc("A"), _tc("B")) + "<w:tr><w:tc>" + _p("외부") + inner + "<w:p/></w:tc>" + _tc("끝") + "</w:tr></w:tbl>")
    t = parse_docx(_docx(outer), "n.docx").text
    assert "| 외부 | 끝 |" in t and "(중첩 표: 2행 1열)" in t and "| 내부1 | 내부2 |" in t
    assert t.index("| 외부 | 끝 |") < t.index("| 내부1 |")


def test_docx_textbox_sdt_header_and_checkbox():
    box = ("<w:p><w:r><mc:AlternateContent><mc:Choice><w:drawing><w:txbxContent>" + _p("텍스트상자") +
           "</w:txbxContent></w:drawing></mc:Choice><mc:Fallback><w:pict><w:txbxContent>" + _p("텍스트상자")
           + "</w:txbxContent></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>")
    sdt = f"<w:sdt><w:sdtPr><w:alias w:val='x'/></w:sdtPr><w:sdtContent>{_p('콘텐츠컨트롤')}</w:sdtContent></w:sdt>"
    chk = ('<w:p><w:r><w:fldChar w:fldCharType="begin"><w:ffData><w:checkBox><w:default w:val="0"/><w:checked/>'
           "</w:checkBox></w:ffData></w:fldChar></w:r><w:r><w:t>동의</w:t></w:r></w:p>")
    t = parse_docx(_docx(box + sdt + chk, header=_p("회사 머리글")), "x.docx").text
    assert t.count("텍스트상자") == 1 and "콘텐츠컨트롤" in t and "☑동의" in t and t.endswith("회사 머리글")


def test_docx_entity_bomb_zip_bomb_and_errors():
    with pytest.raises(ValueError):
        parse_docx(_zip({"word/document.xml": LAUGHS + f"<w:document {WNS}><w:body>{_p('&lol3;')}</w:body></w:document>"}), "l.docx")
    with pytest.raises(ValueError):
        parse_docx(_zip({"word/document.xml": "<a>" + "A" * 5_000_000 + "</a>"}), "b.docx", max_bytes=1_000_000)
    with pytest.raises(ValueError):
        parse_docx(b"nope", "c.docx")
    with pytest.raises(ValueError):
        parse_docx(_zip({"x": "y"}), "d.docx")
    with pytest.raises(ValueError):
        parse_docx(_docx(FORM), "e.docx", max_bytes=50)


def test_docx_pii_masked_in_table(tmp_path):
    body = "<w:tbl>" + _tr(_tc("이름"), _tc("연락처")) + _tr(_tc("홍길동"), _tc("010-1234-5678")) + "</w:tbl>"
    d = parse_docx(_docx(body), "p.docx")
    masked, counts = mask_document(d)
    assert "5678" in d.text and "5678" not in masked.text and counts["phone"] == 1
    s = load_settings({"WIKI_DATA_DIR": str(tmp_path), "WIKI_MASK_PII": "1"})
    r = save_raw(d, "d", s, today=date(2026, 10, 2))
    assert "[PHONE]" in (tmp_path / r.raw_path).read_text(encoding="utf-8")
