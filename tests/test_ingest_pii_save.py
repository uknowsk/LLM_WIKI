from datetime import date

import pytest

from llmwiki.config import load_settings
from llmwiki.ingest.pii import mask
from llmwiki.ingest.save import save_raw
from llmwiki.models import ParsedDocument


def test_mask_all_kinds():
    text = "RRN 900101-1234567, 전화 010-1234-5678 / 02-123-4567, a.b@corp.co.kr, 카드 1234-5678-9012-3456."
    out, counts = mask(text)
    assert counts == {"email": 1, "rrn": 1, "card": 1, "phone": 2}
    for secret in ("1234567", "5678", "corp.co.kr", "9012"):
        assert secret not in out
    assert "[RRN]" in out and "[CARD]" in out


def test_mask_no_false_positive():
    out, counts = mask("금액 12345678901 원, 날짜 2026-09-30")
    assert counts == {} and out.startswith("금액")


def _settings(tmp_path, mask_pii="0"):
    return load_settings({"WIKI_DATA_DIR": str(tmp_path), "WIKI_MASK_PII": mask_pii})


def _doc(**kw):
    return ParsedDocument(title=kw.pop("title", "주간 회의"), text=kw.pop("text", "본문"),
                          source_name="w.eml", metadata=kw.pop("metadata", {"date": "2026-09-30"}))


def test_save_raw_format_and_no_overwrite(tmp_path):
    s = _settings(tmp_path)
    r1 = save_raw(_doc(), "dept-a/part-1", s, today=date(2026, 10, 2))
    r2 = save_raw(_doc(text="다른 본문"), "dept-a/part-1", s, today=date(2026, 10, 2))
    assert r1.raw_path == "raw/dept-a/part-1/2026-09-30-주간-회의.md"
    assert r2.raw_path.endswith("-2.md") and r1.space == "dept-a/part-1"
    content = (tmp_path / r1.raw_path).read_text(encoding="utf-8")
    assert content.startswith("# 주간 회의\n\n> Source: w.eml\n> Collected: 2026-10-02\n> Published: 2026-09-30\n")
    assert len(r1.sha256) == 64


def test_save_raw_unknown_published(tmp_path):
    r = save_raw(_doc(metadata={}), "d", _settings(tmp_path), today=date(2026, 10, 2))
    assert "Published: Unknown" in (tmp_path / r.raw_path).read_text(encoding="utf-8")
    assert r.raw_path.startswith("raw/d/2026-10-02-")


@pytest.mark.parametrize("space", ["", "  ", "..", "a/../b", "../x", "a//b", "a\\b", "/abs", "C:evil"])
def test_save_raw_rejects_bad_space(tmp_path, space):
    with pytest.raises(ValueError):
        save_raw(_doc(), space, _settings(tmp_path))
    assert not (tmp_path / "raw").exists() or not any((tmp_path / "raw").rglob("*.md"))


def test_save_raw_slug_cannot_traverse(tmp_path):
    r = save_raw(_doc(title="../../evil"), "d", _settings(tmp_path))
    assert (tmp_path / r.raw_path).resolve().is_relative_to((tmp_path / "raw").resolve())


def test_save_raw_masking_flag(tmp_path):
    d = _doc(text="전화 010-1234-5678")
    off = save_raw(d, "d", _settings(tmp_path))
    on = save_raw(d, "d", _settings(tmp_path), mask_pii=True)
    dflt = save_raw(d, "d", _settings(tmp_path, "1"))
    assert "010-1234-5678" in (tmp_path / off.raw_path).read_text(encoding="utf-8")
    for r in (on, dflt):
        assert "[PHONE]" in (tmp_path / r.raw_path).read_text(encoding="utf-8")
