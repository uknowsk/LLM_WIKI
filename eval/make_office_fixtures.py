"""Generate synthetic .xlsx / .docx fixtures with merged cells (stdlib only).

Run: .venv/Scripts/python eval/make_office_fixtures.py
Output: eval/corpus/<space>/*.xlsx and *.docx
"""
import datetime
import os
import zipfile
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(ROOT, "corpus")


# ---------------------------------------------------------------- xlsx
def col_letter(i):  # 0-based
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def serial(iso):
    y, m, d = map(int, iso.split("-"))
    return (datetime.date(y, m, d) - datetime.date(1899, 12, 30)).days


STYLE = {"h": 4, "date": 1, "pct": 2, "num": 3, "pct1": 5}

STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/><numFmt numFmtId="165" formatCode="0.0%"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Malgun Gothic"/></font><font><b/><sz val="11"/><name val="Malgun Gothic"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFD9E1F2"/></patternFill></fill></fills>
<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="6">
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>
<xf numFmtId="9" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>
<xf numFmtId="3" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>
</cellXfs>
</styleSheet>"""


def cell_xml(ref, v):
    if v is None:
        return '<c r="%s" s="0"/>' % ref
    if isinstance(v, tuple):
        kind, val = v
        s = STYLE[kind]
        if kind == "h":
            return '<c r="%s" s="%d" t="inlineStr"><is><t>%s</t></is></c>' % (ref, s, escape(val))
        if kind == "date":
            return '<c r="%s" s="%d"><v>%d</v></c>' % (ref, s, serial(val))
        return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, s, repr(val))
    if isinstance(v, (int, float)):
        return '<c r="%s" s="0"><v>%s</v></c>' % (ref, repr(v))
    return '<c r="%s" s="0" t="inlineStr"><is><t>%s</t></is></c>' % (ref, escape(v))


def sheet_xml(rows, merges, widths):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    ncols = max(len(r) for r in rows)
    out.append("<cols>")
    for i in range(ncols):
        w = widths[i] if i < len(widths) else 16
        out.append('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i + 1, i + 1, w))
    out.append("</cols><sheetData>")
    for ri, row in enumerate(rows, 1):
        out.append('<row r="%d">' % ri)
        for ci, v in enumerate(row):
            out.append(cell_xml("%s%d" % (col_letter(ci), ri), v))
        out.append("</row>")
    out.append("</sheetData>")
    if merges:
        out.append('<mergeCells count="%d">' % len(merges))
        for m in merges:
            out.append('<mergeCell ref="%s"/>' % m)
        out.append("</mergeCells>")
    out.append("</worksheet>")
    return "".join(out)


def write_xlsx(path, sheets):
    """sheets: list of (name, rows, merges[, widths])"""
    n = len(sheets)
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i in range(n):
        ct.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1))
    ct.append("</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
    wbrels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i, sh in enumerate(sheets):
        wb.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (escape(sh[0]), i + 1, i + 1))
        wbrels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1))
    wb.append("</sheets></workbook>")
    wbrels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (n + 1))
    wbrels.append("</Relationships>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(ct))
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", "".join(wb))
        z.writestr("xl/_rels/workbook.xml.rels", "".join(wbrels))
        z.writestr("xl/styles.xml", STYLES_XML)
        for i, sh in enumerate(sheets):
            widths = sh[3] if len(sh) > 3 else []
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1), sheet_xml(sh[1], sh[2], widths))


# ---------------------------------------------------------------- docx
class C:
    def __init__(self, text="", span=1, vm=None):
        self.lines = text if isinstance(text, list) else [text]
        self.span = span
        self.vm = vm  # None | 'restart' | 'cont'


def tc_xml(c, width):
    if not isinstance(c, C):
        c = C(c)
    pr = '<w:tcW w:w="%d" w:type="dxa"/>' % (width * c.span)
    if c.span > 1:
        pr += '<w:gridSpan w:val="%d"/>' % c.span
    if c.vm == "restart":
        pr += '<w:vMerge w:val="restart"/>'
    elif c.vm == "cont":
        pr += '<w:vMerge/>'
    paras = "".join('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % escape(t) for t in c.lines)
    return "<w:tc><w:tcPr>%s</w:tcPr>%s</w:tc>" % (pr, paras)


def tbl_xml(rows, ncols, colw=2200):
    out = ['<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>']
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        out.append('<w:%s w:val="single" w:sz="4" w:space="0" w:color="000000"/>' % side)
    out.append("</w:tblBorders></w:tblPr><w:tblGrid>")
    out.append('<w:gridCol w:w="%d"/>' % colw * ncols)
    out.append("</w:tblGrid>")
    for r in rows:
        total = sum((c.span if isinstance(c, C) else 1) for c in r)
        assert total == ncols, (total, ncols, [getattr(c, "lines", c) for c in r])
        out.append("<w:tr>" + "".join(tc_xml(c, colw) for c in r) + "</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def para_xml(t):
    return '<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % escape(t)


def write_docx(path, blocks):
    """blocks: list of str (paragraph) or (rows, ncols) table."""
    body = []
    for b in blocks:
        if isinstance(b, str):
            body.append(para_xml(b))
        else:
            body.append(tbl_xml(b[0], b[1]))
            body.append(para_xml(""))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join(body) + "</w:body></w:document>")
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)


def approval(names, labels):
    """Approval block: first column '결재' merged vertically over 2 rows."""
    n = len(labels) + 1
    return ([[C("결재", vm="restart")] + labels, [C("", vm="cont")] + names], n)


# ---------------------------------------------------------------- fixtures
H = lambda t: ("h", t)
D = lambda t: ("date", t)
P = lambda v: ("pct", v)
P1 = lambda v: ("pct1", v)
N = lambda v: ("num", v)


def build_xlsx():
    out = {}

    # 1. incident status (vertical merge by category, header merge)
    s1 = [
        [H("2026년 3분기 장애 대응 현황표")] + [None] * 6,
        ["작성일", D("2026-10-01"), None, "작성자", "김서연", None, None],
        [H(x) for x in ["구분", "장애ID", "발생일", "시스템", "증상", "복구시간(분)", "담당자"]],
        ["네트워크", "INC-0707", D("2026-07-07"), "CSW-01", "코어 스위치 다운", 33, "정유나"],
        [None, "INC-0805", D("2026-08-05"), "wifi-ap-3F", "무선랜 끊김", 41, "서지안"],
        ["스토리지", "INC-0814", D("2026-08-14"), "nas-bk-03", "백업 볼륨 용량 가득 참", 80, "최민재"],
        [None, "INC-0829", D("2026-08-29"), "nas-fs-01", "파일 읽기 지연", 22, "윤가람"],
        ["애플리케이션", "INC-0903", D("2026-09-03"), "was-prd-02", "메모리 부족(OOM)", 18, "한지우"],
        [None, "INC-0917", D("2026-09-17"), "mail-gw-01", "메일 발송 지연", 54, "이하준"],
        ["합계", None, None, None, None, 248, None],
    ]
    m1 = ["A1:G1", "A4:A5", "A6:A7", "A8:A9", "A10:E10"]
    s1b = [
        [H("장애 분류별 요약")] + [None] * 2,
        [H("분류"), H("건수"), H("평균 복구시간(분)")],
        ["네트워크", 2, 37], ["스토리지", 2, 51], ["애플리케이션", 2, 36], ["합계", 6, 41],
    ]
    out[("dept-a", "incident-status-2026q3.xlsx")] = [("장애대응현황", s1, m1), ("분류별요약", s1b, ["A1:C1"])]

    # 2. monthly budget (group headers 3분기 / 4분기)
    r = lambda label, vals: [label] + [N(v) for v in vals] + [N(sum(vals))]
    srv = [3000, 4000, 5000, 4000, 4000, 3000]
    net = [1000, 1500, 1500, 2000, 1500, 1500]
    sec = [2000, 2500, 2500, 3000, 2500, 2500]
    cld = [0, 2000, 4000, 4000, 2000, 0]
    tot = [a + b + c + d for a, b, c, d in zip(srv, net, sec, cld)]
    s2 = [
        [H("2026년 하반기 IT 인프라 월별 예산표 (단위: 만원)")] + [None] * 7,
        [H("항목"), H("3분기"), None, None, H("4분기"), None, None, H("합계")],
        [None, H("7월"), H("8월"), H("9월"), H("10월"), H("11월"), H("12월"), None],
        r("서버", srv), r("네트워크", net), r("보안(SIEM)", sec), r("클라우드 이전", cld), r("월 소계", tot),
        ["분기 소계", N(sum(tot[:3])), None, None, N(sum(tot[3:])), None, None, N(sum(tot))],
    ]
    m2 = ["A1:H1", "A2:A3", "B2:D2", "E2:G2", "H2:H3", "B9:D9", "E9:G9"]
    s2b = [[H("비고")], ["예비비 3,000만원은 별도 편성하며 합계에 포함하지 않음"],
           ["클라우드 이전 항목은 2026-07-20 예산 메모 기준이며 이후 회의 결정은 반영되지 않음"]]
    out[("dept-a", "budget-monthly-2026h2.xlsx")] = [("월별예산", s2, m2), ("비고", s2b, [])]
    assert sum(tot[:3]) == 29000 and sum(tot[3:]) == 30000

    # 3. inspection checklist with approval block
    s3 = [
        [H("전산실 월간 정기 점검 체크리스트 (2026년 10월)")] + [None] * 7,
        ["점검일", D("2026-10-02"), None, None, H("결재"), H("담당"), H("팀장"), H("본부장")],
        ["점검자", "최민재", None, None, None, "박도윤", "오세훈", "임수정"],
        ["점검구분", "월간 정기", None, None, None, None, None, None],
        [None] * 8,
        [H("구분"), H("점검 항목"), H("기준"), H("결과"), H("판정"), None, None, None],
        ["환경", "항온항습기 온도", "18~24℃", "22.5℃", "정상", None, None, None],
        [None, "서버실 습도", "40~60%", P(0.47), "정상", None, None, None],
        ["전원", "UPS 배터리 용량", "80% 이상", P(0.87), "정상", None, None, None],
        [None, "전원 이중화 상태", "A/B 계통 정상", "정상", "정상", None, None, None],
        ["안전", "소화설비 압력", "정상 범위", "정상", "정상", None, None, None],
        [None, "비상등 점등", "전체 점등", "2개 불량", "조치 필요", None, None, None],
    ]
    m3 = ["A1:H1", "E2:E4", "F3:F4", "G3:G4", "H3:H4", "A7:A8", "A9:A10", "A11:A12"]
    s3b = [[H("조치 내역")] + [None] * 2, [H("항목"), H("조치"), H("완료예정일")],
           ["비상등", "불량 2개 교체", D("2026-10-05")]]
    out[("dept-a", "inspection-checklist-2026-10.xlsx")] = [("월간점검", s3, m3), ("조치내역", s3b, ["A1:C1"])]

    # 4. gantt-like schedule with merged bars
    wk = ["1주", "2주", "3주", "4주"]
    s4 = [
        [H("SIEM 구축 4분기 일정표")] + [None] * 12,
        [H("작업"), H("10월"), None, None, None, H("11월"), None, None, None, H("12월"), None, None, None],
        [None] + [H(w) for w in wk * 3],
        ["로그 소스 연동", H("잔여 17개 연동")] + [None] * 11,
        ["탐지 룰 튜닝"] + [None] * 4 + [H("룰 튜닝 (5주)")] + [None] * 7,
        ["모의 해킹 점검"] + [None] * 7 + [H("외부 업체 점검")] + [None] * 4,
        ["통합 테스트"] + [None] * 8 + [H("통합 테스트 2주")] + [None] * 3,
        ["오픈"] + [None] * 10 + [H("2026-12-15 오픈")] + [None],
    ]
    # fix positions: columns B..M = index 1..12
    s4[3] = ["로그 소스 연동", H("잔여 17개 연동")] + [None] * 11          # B4:G4
    s4[4] = ["탐지 룰 튜닝"] + [None] * 4 + [H("룰 튜닝 (5주)")] + [None] * 7  # F5:J5
    s4[5] = ["모의 해킹 점검"] + [None] * 7 + [H("외부 업체 점검")] + [None] * 4  # I6:K6
    s4[6] = ["통합 테스트"] + [None] * 8 + [H("통합 테스트 2주")] + [None] * 3  # J7:K7
    s4[7] = ["오픈"] + [None] * 10 + [H("2026-12-15 오픈")] + [None]       # L8
    m4 = ["A1:M1", "A2:A3", "B2:E2", "F2:I2", "J2:M2", "B4:G4", "F5:J5", "I6:K6", "J7:K7"]
    out[("dept-a", "schedule-gantt-siem.xlsx")] = [("일정", s4, m4, [16] + [11] * 12)]

    # 5. server asset ledger (vertical merge by center / rack)
    s5 = [
        [H("서버 자산 대장 (2026-10-01 기준)")] + [None] * 6,
        [H(x) for x in ["센터", "랙", "서버명", "용도", "OS", "도입일", "보증 만료일"]],
        ["서울센터", "R01", "db-prd-01", "해든DB 운영 서버", "Linux 8", D("2023-02-15"), D("2026-12-14")],
        [None, None, "db-prd-02", "해든DB 이중화 서버", "Linux 8", D("2023-02-15"), D("2026-12-14")],
        [None, None, "was-prd-01", "그룹웨어 WAS 1", "Linux 8", D("2024-03-10"), D("2029-03-09")],
        [None, "R02", "was-prd-02", "그룹웨어 WAS 2", "Linux 8", D("2024-03-10"), D("2029-03-09")],
        [None, None, "log-srv-02", "로그 수집 서버", "Linux 9", D("2025-01-20"), D("2030-01-19")],
        [None, None, "mail-gw-01", "메일 게이트웨이", "Linux 9", D("2025-06-02"), D("2030-06-01")],
        ["부산센터", "R01", "nas-bk-03", "백업 스토리지", "NAS OS", D("2024-11-05"), D("2029-11-04")],
        [None, None, "nas-fs-01", "파일 서버 스토리지", "NAS OS", D("2022-08-22"), D("2027-08-21")],
        [None, None, "vpn-gw-01", "VPN 게이트웨이", "어플라이언스", D("2023-09-18"), D("2026-09-17")],
    ]
    m5 = ["A1:G1", "A3:A8", "A9:A11", "B3:B5", "B6:B8", "B9:B11"]
    s5b = [
        [H("IP 대역 현황")] + [None] * 3,
        [H(x) for x in ["센터", "용도", "VLAN", "대역"]],
        ["서울센터", "서버", "VLAN 10", "10.10.0.0/24"],
        [None, "관리", "VLAN 20", "10.10.20.0/24"],
        ["부산센터", "서버", "VLAN 10", "10.20.0.0/24"],
        [None, "백업", "VLAN 30", "10.20.30.0/24"],
    ]
    out[("dept-a", "server-asset-ledger.xlsx")] = [("서버대장", s5, m5), ("IP대역", s5b, ["A1:D1", "A3:A4", "A5:A6"])]

    # 6. change request log (two sheets, grouped headers)
    def crs(title, rows):
        return [
            [H(title)] + [None] * 5,
            [H("번호"), H("변경 정보"), None, None, H("승인 정보"), None],
            [None, H("변경일"), H("대상"), H("내용"), H("위험도"), H("승인자")],
        ] + rows
    q2 = crs("2026년 2분기 변경관리 대장", [
        ["CR-0403", D("2026-04-22"), "fw-edge-01", "허용 정책 추가", "낮음", "오세훈"],
        ["CR-0518", D("2026-05-28"), "log-srv-02", "logrotate 설정 반영", "낮음", "오세훈"],
        ["CR-0611", D("2026-06-20"), "vpn-gw-01", "인증서 갱신", "중간", "임수정"],
    ])
    q3 = crs("2026년 3분기 변경관리 대장", [
        ["CR-0702", D("2026-07-25"), "CSW-01", "펌웨어 v9.4.5 적용", "높음", "임수정"],
        ["CR-0809", D("2026-08-23"), "nas-bk-03", "디스크 증설", "중간", "오세훈"],
        ["CR-0925", D("2026-09-26"), "fw-edge-01", "SSH 차단 규칙 추가", "중간", "오세훈"],
    ])
    mm = ["A1:F1", "A2:A3", "B2:D2", "E2:F2"]
    out[("dept-a", "change-request-log-2026.xlsx")] = [("2분기", q2, mm), ("3분기", q3, mm)]

    # 7. license inventory (group headers, percentages)
    s7 = [
        [H("소프트웨어 라이선스 현황 (단위: 만원)")] + [None] * 6,
        [H("제품"), H("유지보수 기간"), None, H("비용"), None, H("활용률"), H("담당자")],
        [None, H("시작일"), H("종료일"), H("2026년"), H("2027년(예정)"), None, None],
        ["해든DB 엔터프라이즈", D("2026-01-01"), D("2026-12-31"), N(8300), N(8800), P(0.92), "박도윤"],
        ["가상화 플랫폼", D("2026-03-01"), D("2027-02-28"), N(2200), N(2400), P(0.78), "이하준"],
        ["백업 소프트웨어", D("2026-05-01"), D("2027-04-30"), N(1250), N(1350), P(0.64), "최민재"],
        ["합계", None, None, N(11750), N(12550), None, None],
    ]
    m7 = ["A1:G1", "A2:A3", "B2:C2", "D2:E2", "F2:F3", "G2:G3", "A7:C7"]
    out[("dept-a", "license-inventory-2026.xlsx")] = [("라이선스", s7, m7, [22, 14, 14, 12, 14, 10, 10])]

    # 8. on-call roster (vertical merged week labels)
    def wk_rows(label, period, day, night):
        return [[label, period, "주간", day[0], day[1]], [None, None, "야간", night[0], night[1]]]
    s8 = [[H("2026년 10월 인프라팀 당직표")] + [None] * 4,
          [H(x) for x in ["주차", "기간", "구분", "당직자", "연락처"]]]
    s8 += wk_rows("10월 1주차", "2026-10-01 ~ 2026-10-07", ("박도윤", "010-0000-1001"), ("이하준", "010-0000-1002"))
    s8 += wk_rows("10월 2주차", "2026-10-08 ~ 2026-10-14", ("최민재", "010-0000-1003"), ("한지우", "010-0000-1004"))
    s8 += wk_rows("10월 3주차", "2026-10-15 ~ 2026-10-21", ("정유나", "010-0000-1005"), ("윤가람", "010-0000-1006"))
    s8 += wk_rows("10월 4주차", "2026-10-22 ~ 2026-10-28", ("서지안", "010-0000-1007"), ("박도윤", "010-0000-1001"))
    m8 = ["A1:E1"] + ["%s%d:%s%d" % (c, r, c, r + 1) for r in (3, 5, 7, 9) for c in "AB"]
    out[("dept-a", "oncall-roster-2026-10.xlsx")] = [("당직표", s8, m8, [14, 26, 8, 12, 16])]

    # 9. dept-b: HR cost budget
    s9 = [
        [H("부서별 인건비 예산표 (단위: 백만원)")] + [None] * 4,
        [H("부서"), H("인건비"), None, None, H("비고")],
        [None, H("상반기 실적"), H("하반기 예산"), H("증감률"), None],
        ["영업본부", N(4120), N(4300), P1(0.044), "영업 인력 5명 증원 반영"],
        ["개발본부", N(5480), N(5800), P1(0.058), "개발 인력 9명 증원 반영"],
        ["관리본부", N(1950), N(2010), P1(0.031), "관리 인력 4명 증원 반영"],
        ["합계", N(11550), N(12110), P1(0.048), None],
    ]
    m9 = ["A1:E1", "A2:A3", "B2:D2", "E2:E3"]
    out[("dept-b", "budget-hr-2026h2.xlsx")] = [("인건비", s9, m9, [14, 14, 14, 12, 26])]
    return out


def build_docx():
    out = {}
    F4 = lambda *a: [list(x) for x in a]

    # 1. server access request
    t = [
        [C("서버 접근 권한 신청서", 4)],
        ["신청자", "이하준", "소속", "인프라팀"],
        ["대상 서버", "db-prd-01", "요청 권한", "읽기 전용"],
        ["사용 기간", C("2026-10-05 ~ 2026-10-31", 3)],
        ["사유", C(["장애 원인 분석을 위한 해든DB 아카이브 로그 조회가 필요합니다.",
                   "조회 대상은 2026년 3월 장애 시점의 로그입니다.",
                   "작업 완료 후 즉시 권한을 회수해 주시기 바랍니다."], 3)],
    ]
    out[("dept-a", "form-server-access-request.docx")] = [(t, 4), approval(["이하준", "오세훈", "임수정"], ["신청자", "팀장", "본부장"])]

    # 2. meeting minutes form
    t = [
        [C("인프라팀 주간회의록", 4)],
        ["일시", "2026-10-01 (목) 10:00", "장소", "본관 4층 소회의실"],
        ["작성자", C("김서연", 3)],
    ]
    att = [
        ["소속", "성명", "직책", "참석"],
        [C("인프라팀", vm="restart"), "오세훈", "팀장", "참석"],
        [C("", vm="cont"), "김서연", "과장", "참석"],
        [C("", vm="cont"), "박도윤", "대리", "참석"],
        [C("보안팀", vm="restart"), "윤가람", "PM", "참석"],
        [C("", vm="cont"), "서지안", "사원", "참석"],
    ]
    out[("dept-a", "minutes-weekly-20261001.docx")] = [
        (t, 4), "참석자", (att, 4), "결정 사항",
        "1. 10월 10일 OS 패치 작업은 계획대로 진행한다. (대상 서버 36대)",
        "2. SIEM 오픈일 2026-12-15은 유지한다.",
        "3. ERP DB 이전은 2027-02-20 일정을 유지한다.",
    ]

    # 3. change request form
    t = [
        [C("변경관리 요청서", 4)],
        ["요청번호", "CR-1015", "요청일", "2026-10-08"],
        ["요청자", "정유나", "위험도", "중간"],
        ["대상 시스템", "fw-edge-01", "변경 일시", "2026-10-15 23:00 ~ 24:00"],
        ["변경 내용", C(["외부 허용 IP 대역 8개 중 2개를 삭제한다.", "협력사 VPN 전용 대역 1개를 신규 허용한다."], 3)],
        ["영향 범위", C("외부 접속 최대 3분 단절, 영향 사용자 약 1,200명", 3)],
        ["롤백 계획", C(["작업 후 20분 내 확인 실패 시 백업 설정 복원", "백업 설정은 22:50에 저장"], 3)],
    ]
    out[("dept-a", "form-change-request-fw-20261015.docx")] = [(t, 4), approval(["정유나", "오세훈", "임수정"], ["요청자", "팀장", "본부장"])]

    # 4. VPN account request
    t = [
        [C("VPN 계정 신청서", 4)],
        ["신청자", "서지안", "소속", "보안팀"],
        ["연락처", "010-0000-3456", "사용 기간", "6개월"],
        ["사용 단말", C("업무용 노트북 (자산번호 NB-2026-0412)", 3)],
        ["사유", C(["재택근무 및 외근 시 사내 시스템 접속",
                   "2026-10-01 시행 비밀번호 정책에 따라 다중 인증(MFA) 단말 등록 필요"], 3)],
    ]
    out[("dept-a", "form-vpn-account-request.docx")] = [(t, 4), approval(["서지안", "윤가람", "오세훈"], ["신청자", "보안 담당", "팀장"])]

    # 5. incident report form (filled)
    t = [
        [C("mail-gw-01 메일 발송 지연 장애 보고서", 4)],
        ["장애ID", "INC-0917", "등급", "3등급"],
        ["발생 일시", "2026-09-17 13:20", "복구 일시", "2026-09-17 14:14"],
        ["복구 시간", "54분", "담당자", "이하준"],
        ["원인", C(["스팸 메일 14만 건이 유입되어 발송 큐가 적체되었다.",
                   "스팸 필터 임계치가 8월 이전 값으로 남아 있었다."], 3)],
        ["조치", C(["스팸 필터 규칙을 갱신하였다.", "큐에 쌓인 스팸 13만 2,000건을 삭제하였다."], 3)],
        ["재발 방지", C("메일 큐 길이가 1만 건을 넘으면 경보를 발송한다.", 3)],
    ]
    out[("dept-a", "form-incident-mailgw-20260917.docx")] = [(t, 4), approval(["이하준", "오세훈", "임수정"], ["작성자", "팀장", "본부장"])]

    # 6. equipment purchase request
    t = [
        [C("장비 구매 신청서", 5)],
        ["신청 부서", C("인프라팀", 2), "신청일", "2026-10-05"],
        ["구매 사유", C(["노후 서버 8대 교체 계획 중 1차분", "백업 스토리지 용량 확장"], 4)],
        ["품목", "규격", "수량", "단가", "금액"],
        ["서버", "2U 랙 서버", "2대", "1,850만원", "3,700만원"],
        ["NAS 디스크", "2TB SSD", "8개", "120만원", "960만원"],
        ["SFP 모듈", "10G", "20개", "20만원", "400만원"],
        [C("합계", 4), "5,060만원"],
    ]
    out[("dept-a", "form-equipment-purchase-202610.docx")] = [(t, 5), approval(["김서연", "오세훈", "임수정"], ["신청자", "팀장", "본부장"])]

    # 7. UPS inspection result
    t = [
        [C("UPS 정기 점검 결과서", 4)],
        ["점검일", "2026-10-02", "점검자", "최민재"],
        ["점검 대상", C("전산실 UPS 2대 (A동, B동)", 3)],
        ["구분", "항목", "측정값", "판정"],
        [C("A동", vm="restart"), "부하율", "41%", "정상"],
        [C("", vm="cont"), "배터리 전압", "13.6V", "정상"],
        [C("B동", vm="restart"), "부하율", "38%", "정상"],
        [C("", vm="cont"), "배터리 전압", "13.1V", "주의"],
        ["종합 의견", C("B동 배터리 모듈 1개의 전압 편차가 커서 제조사에 교환을 요청하고 2주 후 재측정한다.", 3)],
    ]
    out[("dept-a", "form-inspection-ups-202610.docx")] = [(t, 4), approval(["최민재", "오세훈", "임수정"], ["점검자", "팀장", "본부장"])]

    # 8. data restore request
    t = [
        [C("데이터 복구 요청서", 4)],
        ["요청자", "영업팀 강민서", "요청일", "2026-09-29"],
        ["대상 DB", "해든DB ORDERDB", "복구 시점", "2026-09-28 23:00 백업본"],
        ["사유", C(["실수로 주문 이력 테이블의 일부 데이터를 삭제함.",
                   "삭제 건수는 약 4,320건이며 9월 28일 야근 중 발생.",
                   "업무 영향이 커 긴급 복구를 요청함."], 3)],
        ["예상 소요", "3시간", "긴급도", "긴급"],
    ]
    out[("dept-a", "form-data-restore-request.docx")] = [(t, 4), approval(["송재원", "오세훈", "임수정"], ["요청부서장", "인프라팀장", "본부장"])]

    # 9. dept-b leave request
    t = [
        [C("휴가 신청서", 4)],
        ["신청자", "오하은", "소속", "재무팀"],
        ["휴가 구분", "연차", "일수", "3일"],
        ["기간", C("2026-10-12 ~ 2026-10-14", 3)],
        ["사유", C(["가족 여행", "10월 9일까지 업무 인수인계 완료 예정"], 3)],
    ]
    out[("dept-b", "form-leave-request.docx")] = [(t, 4), approval(["오하은", "문서연", "한소율"], ["신청자", "재무팀장", "인사팀"])]

    # 10. dept-b expense report
    t = [
        [C("지출 결의서", 4)],
        ["작성자", "배진호", "작성일", "2026-09-30"],
        ["항목", "내역", "수량", "금액"],
        ["교통비", "KTX 부산 출장", "2명", "112,000원"],
        ["숙박비", "1박", "2명", "216,000원"],
        ["일비", "출장 일비", "2일", "158,000원"],
        [C("합계", 3), "486,000원"],
    ]
    out[("dept-b", "form-expense-report-202609.docx")] = [(t, 4), approval(["배진호", "문서연", "서도현"], ["작성자", "재무팀장", "인사팀장"])]
    return out


def main():
    for (space, name), sheets in build_xlsx().items():
        d = os.path.join(CORPUS, space)
        os.makedirs(d, exist_ok=True)
        write_xlsx(os.path.join(d, name), sheets)
        print("wrote", space, name)
    for (space, name), blocks in build_docx().items():
        d = os.path.join(CORPUS, space)
        os.makedirs(d, exist_ok=True)
        write_docx(os.path.join(d, name), blocks)
        print("wrote", space, name)


if __name__ == "__main__":
    main()
