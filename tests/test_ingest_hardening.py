import codecs
import io
import time
import zipfile
from datetime import date
from email.message import EmailMessage

import pytest

from llmwiki.config import load_settings
from llmwiki.ingest.docx import parse_docx
from llmwiki.ingest.eml import html_to_text, parse_eml
from llmwiki.ingest.pdf import parse_pdf
from llmwiki.ingest.pii import _PATTERNS, mask
from llmwiki.ingest.save import save_raw, validate_space
from llmwiki.ingest.text import decode_bytes, parse_text
from llmwiki.models import ParsedDocument


def _s(tmp_path, m="0"):
    return load_settings({"WIKI_DATA_DIR": str(tmp_path), "WIKI_MASK_PII": m})


def _doc(**kw):
    return ParsedDocument(title=kw.pop("title", "t"), text=kw.pop("text", "본문"),
                          source_name=kw.pop("source_name", "w.eml"), metadata=kw.pop("metadata", {"date": "2026-09-30"}))


# ---- space validation ----
@pytest.mark.parametrize("space", ["Dept-A", "dept-a.", "con", "nul", "a ", "x\\..", "a/COM1", "a/lpt9", "a/b/c/d/e",
                                   "-a", "a_b", "é", "a\n", "x" * 64, "a/ b"])
def test_validate_space_rejects(space):
    with pytest.raises(ValueError):
        validate_space(space)


def test_validate_space_accepts():
    assert validate_space("dept-a/part-1") == ["dept-a", "part-1"]
    assert validate_space("a/b/c/d") == ["a", "b", "c", "d"]


@pytest.mark.parametrize("space", ["Dept-A", "con", "x\\.."])
def test_save_raw_bad_space_is_valueerror(tmp_path, space):
    with pytest.raises(ValueError):
        save_raw(_doc(), space, _s(tmp_path))


# ---- PII ----
@pytest.mark.parametrize("text,kind", [
    ("+82 10-1234-5678", "phone"), ("+82-10-1234-5678", "phone"), ("82 10 1234 5678", "phone"),
    ("(02) 123-4567", "phone"), ("010 1234 5678", "phone"), ("010-1234​-5678", "phone"),
    ("０１０-１２３４-５６７８", "phone"),
    ("900101–1234567", "rrn"), ("900101− 1234567", "rrn"),
    ("3782 822463 10005", "card"), ("378282246310005", "card"),
    ("123-45-67890", "brn"), ("a​b@corp.com", "email"),
])
def test_mask_variants(text, kind):
    out, counts = mask(f"x {text} y")
    assert counts.get(kind) == 1, (out, counts)
    assert not any(ch.isdigit() for ch in out)


def test_mask_email_no_redos():
    t0 = time.perf_counter()
    _PATTERNS[0][2].findall("a" * 30000)
    assert time.perf_counter() - t0 < 0.05
    t0 = time.perf_counter()
    mask("a" * 30000)
    mask("a." * 15000)
    assert time.perf_counter() - t0 < 0.5


def test_save_masks_header_and_slug(tmp_path):
    d = _doc(title="문의 010-1234-5678", source_name="010-1234-5678_홍길동.docx",
             metadata={"date": "2026-09-30", "from": "kim@corp.com"})
    r = save_raw(d, "d", _s(tmp_path, "1"), today=date(2026, 10, 2))
    body = (tmp_path / r.raw_path).read_text(encoding="utf-8")
    assert "5678" not in body and "5678" not in r.raw_path and "홍길동" in body
    assert "[PHONE]" in body


# ---- header / dedupe ----
def test_header_cannot_be_forged(tmp_path):
    d = _doc(title="x\n> Published: 1999-01-01", source_name="a\r\n> Collected: 1999-01-01.eml",
             metadata={"date": "2026-09-30\n"})
    r = save_raw(d, "d", _s(tmp_path), today=date(2026, 10, 2))
    lines = (tmp_path / r.raw_path).read_text(encoding="utf-8").split("\n")
    assert sum(ln.startswith("> Published:") for ln in lines) == 1
    assert sum(ln.startswith("> Collected:") for ln in lines) == 1
    assert "> Published: Unknown" in lines  # trailing newline in date is not a valid date


def test_identical_content_returns_existing(tmp_path):
    s = _s(tmp_path)
    a = save_raw(_doc(), "d", s, today=date(2026, 10, 2))
    b = save_raw(_doc(), "d", s, today=date(2026, 10, 2))
    assert a == b and len(list((tmp_path / "raw" / "d").glob("*.md"))) == 1
    c = save_raw(_doc(text="다름"), "d", s, today=date(2026, 10, 2))
    assert c.raw_path.endswith("-2.md")


# ---- eml ----
def _raw_eml(body: bytes, charset: str) -> bytes:
    return (b"From: a@x.com\r\nTo: b@x.com\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: text/plain; charset=" + charset.encode() + b"\r\nContent-Transfer-Encoding: 8bit\r\n\r\n" + body)


@pytest.mark.parametrize("charset", ["utf-8", "ks_c_5601-1987", "euc-kr", "bogus-charset"])
def test_eml_cp949_bytes_any_label(charset):
    r = parse_eml(_raw_eml("한글 본문 입니다".encode("cp949"), charset), "a.eml")
    assert r.document.text == "한글 본문 입니다"


def test_eml_garbage_replaced():
    r = parse_eml(_raw_eml(b"ok \xff\xfe\xfd", "utf-8"), "a.eml")
    assert r.document.text.startswith("ok")


def test_html_tables_and_skips():
    t = html_to_text("<td>100</td><td>200</td>")
    assert "100" in t and "200" in t and "100200" not in t
    t = html_to_text("<html><head><title>T</title><style>x{}</style></head><body><ul><li>a</li><li>b</li></ul></body></html>")
    assert t == "a\n\nb"


def test_eml_attachment_names_unique_and_nested():
    m = EmailMessage()
    m["Subject"] = "s"
    m.set_content("body")
    m.add_attachment(b"1", maintype="application", subtype="octet-stream", filename="a.pdf")
    m.add_attachment(b"2", maintype="application", subtype="octet-stream", filename="a.pdf")
    m.add_attachment(b"3", maintype="application", subtype="octet-stream")
    m.add_attachment(b"4", maintype="application", subtype="octet-stream")
    m.add_attachment(b"5", maintype="application", subtype="octet-stream", filename="..\\..\\x.bin")
    inner = EmailMessage()
    inner["Subject"] = "fwd subj"
    inner["From"] = "z@x.com"
    inner.set_content("중첩 본문")
    m.add_attachment(inner)
    r = parse_eml(m.as_bytes(), "a.eml")
    names = [n for n, _ in r.attachments]
    assert len(set(names)) == len(names)
    assert names[:2] == ["a.pdf", "a-2.pdf"] and "attachment-1.bin" in names and "attachment-2.bin" in names
    assert "x.bin" in names
    fwd = dict(r.attachments)["forwarded-1.txt"].decode()
    assert "중첩 본문" in fwd and "fwd subj" in fwd


# ---- text ----
@pytest.mark.parametrize("enc", ["utf-16", "utf-32", "utf-8-sig"])
def test_decode_bom(enc):
    assert decode_bytes("안녕 hello".encode(enc)) == "안녕 hello"
    assert parse_text("# 제목\n본문".encode(enc), "a.md").title == "제목"


def test_decode_utf16_be_bom():
    assert decode_bytes(codecs.BOM_UTF16_BE + "한글".encode("utf-16-be")) == "한글"


# ---- limits ----
def test_size_limits():
    for fn, name in ((parse_text, "a.txt"), (parse_eml, "a.eml"), (parse_pdf, "a.pdf"), (parse_docx, "a.docx")):
        with pytest.raises(ValueError):
            fn(b"x" * 11, name, max_bytes=10)


def test_docx_zip_bomb_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<a>" + "A" * 5_000_000 + "</a>")
    assert len(buf.getvalue()) < 100_000
    with pytest.raises(ValueError):
        parse_docx(buf.getvalue(), "bomb.docx", max_bytes=1_000_000)


def test_docx_filename_with_phone_masked(tmp_path):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {ns}><w:body><w:p><w:r><w:t>x</w:t></w:r></w:p></w:body></w:document>")
    d = parse_docx(buf.getvalue(), "010-1234-5678_홍길동.docx")
    r = save_raw(d, "d", _s(tmp_path, "1"), today=date(2026, 10, 2))
    assert "5678" not in (tmp_path / r.raw_path).read_text(encoding="utf-8") and "5678" not in r.raw_path
