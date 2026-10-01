import io
import zipfile
from email.message import EmailMessage

import pytest

from llmwiki.ingest.docx import parse_docx
from llmwiki.ingest.eml import html_to_text, parse_eml, thread_key
from llmwiki.ingest.pdf import FakeOcrEngine, PdfExtractor, parse_pdf
from llmwiki.ingest.text import parse_text


def _eml(**headers) -> EmailMessage:
    m = EmailMessage()
    m["From"] = "Kim <kim@example.com>"
    m["To"] = "lee@example.com"
    m["Date"] = "Wed, 30 Sep 2026 09:15:00 +0900"
    m["Message-ID"] = "<m2@example.com>"
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    return m


def test_eml_korean_charset_and_metadata():
    m = _eml(Subject="주간 회의록", Cc="park@example.com",
             In_Reply_To="<m1@example.com>", References="<m0@example.com> <m1@example.com>")
    m.set_content("안녕하세요. 회의 내용입니다.", charset="euc-kr")
    r = parse_eml(m.as_bytes(), "w.eml")
    assert r.document.title == "주간 회의록"
    assert "회의 내용" in r.document.text
    md = r.document.metadata
    assert md["date"] == "2026-09-30"
    assert md["cc"] == "park@example.com"
    assert md["message_id"] == "<m2@example.com>"
    assert md["references"] == "<m0@example.com> <m1@example.com>"
    assert thread_key(md) == "m0@example.com"


def test_eml_html_only_and_attachment():
    m = _eml(Subject="html")
    m.set_content("x")
    m.clear_content()
    m.set_content("<html><style>p{}</style><body><p>첫째</p><p>둘째 &amp; 셋째</p></body></html>", subtype="html")
    m.add_attachment(b"%PDF-fake", maintype="application", subtype="pdf", filename="../보고서.pdf")
    r = parse_eml(m.as_bytes(), "h.eml")
    assert "첫째" in r.document.text and "둘째 & 셋째" in r.document.text
    assert "<p>" not in r.document.text and "p{}" not in r.document.text
    assert r.attachments == [("보고서.pdf", b"%PDF-fake")]


def test_thread_key_fallbacks():
    assert thread_key({"in_reply_to": "<A@x>", "message_id": "<b@x>"}) == "a@x"
    assert thread_key({"message_id": "<B@x>"}) == "b@x"
    assert thread_key({}) == ""


def test_html_to_text_blocks():
    assert html_to_text("<div>a</div><div>b</div>") == "a\n\nb"


def test_text_md_title_and_cp949():
    d = parse_text("# 제목\n\n본문\r\n".encode("utf-8"), "a.md")
    assert d.title == "제목" and "본문" in d.text
    d2 = parse_text("한글 문서".encode("cp949"), "note.txt")
    assert d2.title == "note" and d2.text == "한글 문서"


def _docx(paragraphs: list[str], title: str | None = None) -> bytes:
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {ns}><w:body>{body}</w:body></w:document>")
        if title:
            z.writestr(
                "docProps/core.xml",
                '<cp:coreProperties xmlns:cp="x" xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:dcterms="http://purl.org/dc/terms/">'
                f"<dc:title>{title}</dc:title><dcterms:created>2026-09-01T00:00:00Z</dcterms:created>"
                "</cp:coreProperties>",
            )
    return buf.getvalue()


def test_docx_parse():
    d = parse_docx(_docx(["첫 문단", "둘째"], title="보고서"), "r.docx")
    assert d.title == "보고서" and d.text == "첫 문단\n\n둘째"
    assert d.metadata["date"] == "2026-09-01"
    assert parse_docx(_docx(["x"]), "plain.docx").title == "plain"


def test_docx_invalid():
    with pytest.raises(ValueError):
        parse_docx(b"not a zip", "bad.docx")


class _Pages:
    def __init__(self, pages):
        self.pages = pages

    def extract_pages(self, data):
        return self.pages


def test_pdf_ocr_fallback_only_for_empty_pages():
    ocr = FakeOcrEngine({1: "스캔 페이지"})
    ex = _Pages(["텍스트 레이어", "  "])
    assert isinstance(ex, PdfExtractor)
    d = parse_pdf(b"x", "s.pdf", extractor=ex, ocr=ocr)
    assert ocr.calls == [1]
    assert d.text == "텍스트 레이어\n\n스캔 페이지"
    assert d.metadata["ocr_pages"] == "2"


def test_pdf_without_ocr_flags_empty_pages():
    d = parse_pdf(b"x", "s.pdf", extractor=_Pages(["", "a"]))
    assert d.metadata["empty_pages"] == "1" and d.text == "a"
